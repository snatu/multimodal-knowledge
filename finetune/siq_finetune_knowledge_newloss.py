"""
Finetunes TVQA.

I used a v3-32 for this (it's more expensive than VCR due to the use of video + sound data)
"""

import shutil
import sys

sys.path.append('../../')
sys.path.append('/home/sheryl/merlot_reserve')
import yaml
from datetime import datetime
import pytz
import jax
import jax.numpy as jnp
from pretrain.dataloader import input_fn_builder, MASK, encoder, AUDIOSPAN
from finetune.common_dataloader_knowledge_newloss import finetune_input_fn_builder, finetune_val_input_fn_builder
from mreserve.modeling import MerlotReserve

from flax.training import train_state
from flax import jax_utils
import flax.linen as nn
from finetune.optimization import construct_finetuning_train_state, finetune_train_step
from mreserve.checkpoint import save_checkpoint, load_checkpoint, bf16_to_f32, f32_to_bf16
import argparse
import pandas as pd
import numpy as np
from flax.core.frozen_dict import freeze
from copy import deepcopy
import clu.parameter_overview
import functools
import time
import os
from dotenv import load_dotenv
import wandb
from mreserve.modeling import PretrainedMerlotReserve

load_dotenv('../../.env')

jax.config.update('jax_log_compiles', True)
is_on_gpu = any([x.platform == 'gpu' for x in jax.local_devices()])
print('JAX process: {} / {}. Local devices {}. Using {}'.format(jax.process_index(), jax.process_count(), jax.local_devices(), 'GPU' if is_on_gpu else 'TPU'), flush=True)

parser = argparse.ArgumentParser(description='Train model!')

parser.add_argument(
    '-pretrain_config_file',
    help='Where the config.yaml is located',
    type=str,
    default="../../pretrain/configs/base.yaml",
)
parser.add_argument(
    '-ckpt',
    help='checkpoint to use',
    type=str,
    default="../../../base_resadapt",
)
parser.add_argument(
    '--lr',
    help='lr',
    type=float,
    default=5e-6,
)
parser.add_argument(
    '-ne',
    help='ne',
    type=int,
    default=20,
)
parser.add_argument(
    '-output_grid_h',
    help='output_grid_h',
    type=int,
    default=18,
)
parser.add_argument(
    '-output_grid_w',
    help='output_grid_w',
    type=int,
    default=32,
)
parser.add_argument(
    '-output_name',
    help='output_name',
    type=str,
    default='',
)
parser.add_argument(
    '-wandb_name',
    help='wandb_name',
    type=str,
    default='siq-finetune',
)
parser.add_argument(
    '-val_batch_size',
    help='val_batch_size -- defaults to 32',
    type=int,
    default=32,
)
parser.add_argument(
    '-scan_minibatch',
    help='scan_minibatch -- basically, if this is true then batch size is 1 but we do gradient accumulation',
    action='store_true',
    default=True,
)
args = parser.parse_args()

# print(f"Loading from {args.config_file}", flush=True)
with open(args.pretrain_config_file, 'r') as f:
    config = yaml.load(f, yaml.FullLoader)
os.environ["TFRECORDS_PATH"] =  "/data/raw/siq_external_knowledge"
os.environ["OUTPUT_PATH"] = "/data/raw/models"
config['data']['train_fns'] = os.path.join(os.environ["TFRECORDS_PATH"], "train{:03d}of833.tfrecord")
config['data']['num_train_files'] = 833
config['data']['num_answers'] = 4
config['data']['random_scale_max'] = 1.1
config['data']['random_scale_min'] = 1.0
config['data']['num_segments'] = 7

config['device']['batch_size'] = 8
config['device']['prefetch_size'] = 0
config['device']['n_fns_per_cycle'] = 833

NUM_EPOCH = args.ne
TRAIN_SIZE = 21 * config['data']['num_train_files']
steps_per_epoch = TRAIN_SIZE // config['device']['batch_size']
config['optimizer'] = {
    'beta_2': 0.98,
    'eps': 1e-6,
    'learning_rate': args.lr,
    'num_train_steps': NUM_EPOCH * steps_per_epoch,
    'num_warmup_steps': int(0.5 * steps_per_epoch),
    'use_bfloat16_adam': True,
    'weight_decay_rate': 0.1,
    'do_bias_correction': True,
}
print(args.ckpt)
config['device']['iterations_per_loop'] = steps_per_epoch
config['data']['lang_seq_len'] = 256
cfg_name = args.pretrain_config_file.split('/')[-1]
seattle_time = pytz.utc.localize(datetime.utcnow()).astimezone(pytz.timezone('America/Los_Angeles'))
seattle_time = seattle_time.strftime("%Y-%m-%d-%H:%M.%S")

config['device']['output_dir'] = os.path.join(os.environ["OUTPUT_PATH"], cfg_name)
if args.output_name != '':
    config['device']['output_dir'] = os.path.join(config['device']['output_dir'], args.output_name)
config['device']['output_dir'] = os.path.join(config['device']['output_dir'], seattle_time)

np.random.seed(123456)
config['model']['output_grid'] = [args.output_grid_h, args.output_grid_w]
ds_train_iter = finetune_input_fn_builder(config, 'tvqa')
# _, dummy_batch = next(ds_train_iter)


config['_ckpt'] = args.ckpt
tags = [cfg_name]
if args.output_name != '':
    tags.append(args.output_name)
# if (jax.process_index() == 0):
#     import wandb
#wandb.init(config=config, project=args.wandb_name, entity='sherylm', notes="Loaded from "+cfg_name, tags=tags)
# else:
wandb = None

class MerlotReserveTVQA(MerlotReserve):
    def setup(self):
        super().setup()
        self.proj = nn.Dense(features=1, dtype=self.dtype, kernel_init=jax.nn.initializers.normal(stddev=0.02), name='proj',
                             use_bias=False)

    def __call__(self, batch):

        # Encode images (twice)
        batch_size, images_per_batch, seq_size, img_dim = batch['images'].shape
        imgs_enc = self.vision_encoder(batch['images'].reshape(batch_size * images_per_batch, seq_size, img_dim))['seq_attnpool']
        imgs_enc = imgs_enc.reshape(batch_size, images_per_batch, seq_size // 4, self.hidden_size)

        # Add the "first image"
        imgs_enc = jnp.concatenate([
            jnp.zeros([batch_size, 1, seq_size // 4, self.hidden_size], dtype=imgs_enc.dtype),
            imgs_enc,
        ], 1)

        # duplicate so that we have one per answer
        images_per_batch += 1
        batch_size, num_ans_per, joint_seq_len, two_ = batch['textonly_seqs'].shape
        imgs_enc = imgs_enc.reshape(batch_size, images_per_batch * seq_size // 4, self.hidden_size).repeat(num_ans_per, axis=0)

        #########################
        text_toks = batch['textonly_seqs'][..., 0].reshape(batch_size * num_ans_per, joint_seq_len)
        textonly_inputs = self.prepare_multimodal_inputs(
            tokens=text_toks,
            token_segment_idx=batch['textonly_seqs'][..., 1].reshape(batch_size * num_ans_per, joint_seq_len),
            vision_input=imgs_enc,
        )
        
        #########################
        knowledge_toks = batch['knowledge'][..., 0].reshape(-1, joint_seq_len)
        knowledgeonly_inputs = self.prepare_multimodal_inputs(
            tokens=knowledge_toks,
            token_segment_idx=batch['knowledge'][..., 1].reshape(-1, joint_seq_len),
            vision_input=imgs_enc,
        )

        # Encode audio
        # Audio clips are provided as [batch_size, num_segments, num_audio_subsegments, audio_seq_len, num_mels]
        batch_size, num_segments, num_audio_subsegments, audio_seq_len, num_mels = batch['audio_clips'].shape
        audio_enc = self.audio_encoder(batch['audio_clips'].reshape(-1, audio_seq_len, num_mels))['seq_attnpool']

        _, audio_token_len, hidden_size = audio_enc.shape
        num_audio_spans = num_segments * num_audio_subsegments

        audio_enc = audio_enc.reshape(batch_size, num_audio_spans, audio_token_len, hidden_size)
        audio_enc = audio_enc.repeat(num_ans_per, axis=0)

        audio_toks = batch['audio_seqs'][..., 0].reshape(batch_size * num_ans_per, joint_seq_len)
        audio_pointers = (jnp.cumsum((audio_toks == AUDIOSPAN).astype(jnp.int32), -1) - 1) // audio_token_len
        audio_pointers = audio_pointers % num_audio_spans

        audio_inputs = self.prepare_multimodal_inputs(
            tokens=batch['audio_seqs'][..., 0].reshape(batch_size * num_ans_per, joint_seq_len),
            token_segment_idx=batch['audio_seqs'][..., 1].reshape(batch_size * num_ans_per, joint_seq_len),
            vision_input=imgs_enc,
            audio_spans=audio_enc,
            audio_pointers=audio_pointers,
        )
        # hack: remove 'first img' from sequence lengths
        start_imgs = joint_seq_len + seq_size // 4
        for k in ['x', 'rotary_coords', 'attention_mask']:
            textonly_inputs[k] = jnp.concatenate([textonly_inputs[k][:, :joint_seq_len],
                                                  textonly_inputs[k][:, start_imgs:]], 1)
            
            knowledge_inputs[k] = jnp.concatenate([knowledge_inputs[k][:, :joint_seq_len],
                                      knowledge_inputs[k][:, start_imgs:]], 1)

            audio_inputs[k] = jnp.concatenate([audio_inputs[k][:, :joint_seq_len],
                                               audio_inputs[k][:, start_imgs:]], 1)


        textonly_inputs['attention_mask'] = jnp.concatenate([textonly_inputs['attention_mask'][:, :, :joint_seq_len],
                                                             textonly_inputs['attention_mask'][:, :, start_imgs:]], 2)
        
        knowledge_inputs['attention_mask'] = jnp.concatenate([knowledge_inputs['attention_mask'][:, :, :joint_seq_len],
                                                     knowledge_inputs['attention_mask'][:, :, start_imgs:]], 2)

        audio_inputs['attention_mask'] = jnp.concatenate([audio_inputs['attention_mask'][:, :, :joint_seq_len],
                                                          audio_inputs['attention_mask'][:, :, start_imgs:]], 2)
        #############################################################################################################

        # if args.disable_audio:
        #     x = textonly_inputs['x']
        #     coords = textonly_inputs['rotary_coords']
        #     attnmask = textonly_inputs['attention_mask']
        #     joint_enc = self.joint_transformer(x, rotary_coords=coords, attention_mask=attnmask)['seq']
        #     joint_enc = joint_enc[:, :joint_seq_len].reshape(batch_size * num_ans_per, joint_seq_len, self.hidden_size)
        #
        #     pool_idx = jnp.argmax((text_toks == MASK).astype(jnp.float32), 1)
        #     pooled_h = joint_enc[jnp.arange(batch_size * num_ans_per), pool_idx]
        #     logits_from_text = jnp.squeeze(self.proj(pooled_h), -1)
        #     logits_from_text = logits_from_text.reshape(batch_size, num_ans_per)
        #     logits_from_audio = jnp.ones_like(logits_from_text)
        #     return logits_from_audio, logits_from_text


        x = jnp.concatenate([audio_inputs['x'], textonly_inputs['x']], 0)
        coords = jnp.concatenate([audio_inputs['rotary_coords'], textonly_inputs['rotary_coords']], 0)
        attnmask = jnp.concatenate([audio_inputs['attention_mask'], textonly_inputs['attention_mask']], 0)

        joint_enc = self.joint_transformer(x, rotary_coords=coords, attention_mask=attnmask)['seq']
        joint_enc = joint_enc[:, :joint_seq_len].reshape(batch_size * 2 * num_ans_per, joint_seq_len, self.hidden_size)

        # Pool from the right tokens
        pool_idx = jnp.argmax((jnp.concatenate([audio_toks, text_toks, knowledge_toks], 0) == MASK).astype(jnp.float32), 1)
        pooled_h = joint_enc[jnp.arange(batch_size * 2 * num_ans_per), pool_idx]
        joint_enc = jnp.squeeze(self.proj(pooled_h), -1)

        logits_from_audio, logits_from_text, logits_from_knowledge = jnp.split(joint_enc, 3, axis=0)
        logits_from_audio = logits_from_audio.reshape(batch_size, num_ans_per)
        logits_from_text = logits_from_text.reshape(batch_size, num_ans_per)
        logits_from_knowledge = logits_from_knowledge.reshape(batch_size, num_ans_per)

        return logits_from_audio, logits_from_text, logits_from_knowledge


model = MerlotReserveTVQA.from_config(config)
grid_size = (12, 20)
#pretrained_model = PretrainedMerlotReserve.from_pretrained(model_name='large', image_grid_size=grid_size)
# if args.ckpt == '':
#     params = model.init_from_dummy_batch(dummy_batch).unfreeze()
# else:
params = load_checkpoint(args.ckpt)['params']

# Don't need those
for k in ['head', 'span_encoder']:
    params.pop(k, None)
hsz = params['joint_transformer']['final_ln']['bias'].shape[0]
params['proj'] = {'kernel': np.random.randn(hsz, 1).astype(np.float32) * 0.01}
params = freeze(params)

state, tx_fns = construct_finetuning_train_state(opt_config=config['optimizer'], model=model, params=params)

def train_loss_fn(state, params, batch):
    logits_from_audio, logits_from_text, logits_from_knowledge = state.apply_fn({'params': params}, batch)
    lprobs_from_audio = jax.nn.log_softmax(logits_from_audio, axis=-1)
    lprobs_from_text = jax.nn.log_softmax(logits_from_text, axis=-1)
    lprobs_from_knowledge = jax.nn.log_softmax(logits_from_text, axis=-1)

    labels_oh = jax.nn.one_hot(batch['labels'],
                               dtype=logits_from_audio.dtype,
                               num_classes=logits_from_audio.shape[-1])

    loss_audio = -jnp.mean(jnp.sum(labels_oh * lprobs_from_audio, axis=-1))
    loss_text = -jnp.mean(jnp.sum(labels_oh * lprobs_from_text, axis=-1))
    loss_knowledge = -jnp.mean(jnp.sum(labels_oh * lprobs_from_knowledge, axis=-1))

    loss = loss_audio + loss_text + loss_knowledge
    is_right_audio = (jnp.argmax(logits_from_audio, -1) == batch['labels']).astype(jnp.float32).mean()
    is_right_text = (jnp.argmax(logits_from_text, -1) == batch['labels']).astype(jnp.float32).mean()
    is_right_knowledge = (jnp.argmax(logits_from_knowledge, -1) == batch['labels']).astype(jnp.float32).mean()


    return loss, {'train_audio_acc': is_right_audio, 'train_text_acc': is_right_text, 'train_knowledge_acc': is_right_knowlege,
                  'train_audio_loss': loss_audio, 'train_text_loss': loss_text, 'train_knowledge_loss': loss_knowlege}


p_train_step = jax.pmap(functools.partial(finetune_train_step, loss_fn=train_loss_fn, tx_fns=tx_fns, scan_minibatch=args.scan_minibatch),
                                          axis_name='batch', donate_argnums=(0,1))

def pred_step(state: train_state.TrainState, batch):
    logits_from_audio, logits_from_text, logits_from_knowledge = state.apply_fn({'params': state.params}, batch)

    out = {'logprobs_audio': jax.nn.log_softmax(logits_from_audio, axis=-1),
            'preds_audio': jnp.argmax(logits_from_audio, -1),
            'logprobs_text': jax.nn.log_softmax(logits_from_text, axis=-1),
            'preds_text': jnp.argmax(logits_from_text, -1),
            'logprobs_knowledge': jax.nn.log_softmax(logits_from_knowledge, axis=-1),
            'preds_knowledge': jnp.argmax(logits_from_knowledge, -1),
            }
    softmax_joint = jax.nn.softmax(logits_from_audio, axis=-1) + jax.nn.softmax(logits_from_text, axis=-1) + jax.nn.softmax(logits_from_knowledge, axis=-1)
    out['preds_joint'] = jnp.argmax(softmax_joint, -1)
    return out


p_pred_step = jax.pmap(pred_step, axis_name='batch', donate_argnums=(1,))


def val_epoch(state: train_state.TrainState):
    """
    perform a validation epoch
    :param state:
    :return:
    """
    val_config = deepcopy(config)
    val_config['data']['val_fns'] = os.path.join(os.environ["TFRECORDS_PATH"], "val{:03d}of061.tfrecord")
    val_config['data']['num_val_files'] = 61
    val_config['data']['do_random_scale'] = False
    val_config['data']['batch_size'] = args.val_batch_size

    val_iter = finetune_val_input_fn_builder(val_config, 'tvqa')

    text_preds = []
    audio_preds = []
    joint_preds = []
    batch_num = 0
    for ids, batch in val_iter:
        if batch_num % 50 == 0:
            print("~~~~VAL_BATCH", batch_num)
        batch_num+=1
        val_pred = p_pred_step(state, batch)
        preds_joint = val_pred['preds_joint'].reshape(-1)
        preds_audio = val_pred['preds_audio'].reshape(-1)
        preds_text = val_pred['preds_text'].reshape(-1)
        preds_knowledge = val_pred['preds_knowledge'].reshape(-1)


        labels = batch['labels'].reshape(-1)
        for (p_j, p_a, p_t, p_k, id_i, label_i) in zip(val_pred['preds_joint'].reshape(-1),
                                                  val_pred['preds_audio'].reshape(-1),
                                                  val_pred['preds_text'].reshape(-1), val_pred['preds_knowledge'].reshape(-1), ids, labels):
            if id_i == 'pad':
                continue
            text_preds.append({'pred': p_t, 'label': label_i, 'id': id_i})
            audio_preds.append({'pred': p_a, 'label': label_i, 'id': id_i})
            joint_preds.append({'pred': p_j, 'label': label_i, 'id': id_i})
            knowledge_preds.append({'pred': p_k, 'label': label_i, 'id': id_i})


    text_preds = pd.DataFrame(text_preds)
    text_preds['is_right'] = text_preds['pred'] == text_preds['label']
    text_acc = text_preds['is_right'].mean()
    
    knowledge_preds = pd.DataFrame(knowledge_preds)
    knowledge_preds['is_right'] = knowledge_preds['pred'] == knowledge_preds['label']
    knowledge_acc = knowledge_preds['is_right'].mean()

    audio_preds = pd.DataFrame(audio_preds)
    audio_preds['is_right'] = audio_preds['pred'] == audio_preds['label']
    audio_acc = audio_preds['is_right'].mean()

    joint_preds = pd.DataFrame(joint_preds)
    joint_preds['is_right'] = joint_preds['pred'] == joint_preds['label']
    joint_acc = joint_preds['is_right'].mean()
    if wandb is not None:
       wandb.log({'val_joint_acc': joint_acc, 'val_text_acc': text_acc, 'val_audio_acc': audio_acc})
    return {'text_acc': text_acc, 'audio_acc': audio_acc, 'joint_acc': joint_acc, 'knowledge_acc': knowledge_acc}

train_metrics = []
log_every = config['device'].get('commit_every_nsteps', 50)
time_elapsed = []
num_batch = 0

# the + 1 is because for some reason it crashes at the end otherwise. why? idk/
for n in range(config['optimizer']['num_train_steps']+100):
    st = time.time()
    id_, batch = next(ds_train_iter)
    if num_batch % 50 == 0:
        print("~~~~~~BATCH", num_batch)
    num_batch += 1
    state, loss_info = p_train_step(state, batch)

    if jax.process_index() == 0:
        train_metrics.append(jax.tree_map(lambda x: x[0], loss_info))
        jax.tree_map(lambda x: x.copy_to_host_async(), train_metrics[-1])

        step_for_logging = n - log_every
        if step_for_logging >= 0:
            train_metrics[step_for_logging] = {k: float(v) for k, v in train_metrics[step_for_logging].items()}
            if wandb is not None:
                wandb.log(train_metrics[step_for_logging], step=step_for_logging, commit=True) #(n + 1) % log_every == 0)

        # if (n + 1) % config['device']['iterations_per_loop'] == 0:
        if (n + 1) % 50 == 0: # do val every 500 steps
            print("Done 500 steps, doing validation", flush=True)

            save_checkpoint(state, path=config['device']['output_dir'], no_optimizer=True)
            val_info = val_epoch(state)
            print(f"Saving @iter {n:03d}.\nInfo: {pd.Series(val_info)}\n~\n", flush=True)
            if wandb is not None:
                wandb.log({'joint_acc_val': val_info['joint_acc']}, step=step_for_logging, commit=True)
                # wandb.log({k + '_val': v for k, v in val_info.items()}, step=step_for_logging, commit=True)
            #shutil.rmtree("/data/merlot_reserve/out/base.yaml/")

        time_elapsed.append(time.time() - st)
        if len(time_elapsed) >= 100:
            tsum = sum(time_elapsed)
            print("Completed 100 batches in {:.3f}sec, avg {:.3f} it/sec".format(tsum, 100.0 / tsum), flush=True)
            time_elapsed = []
