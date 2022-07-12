#! /usr/bin/bash

# cd finetune/tvqa
python3 tvqa_finetune_alexander.py ../../pretrain/configs/base.yaml  ../../base.ckpt -lr=5e-6 -ne=3 -scan_minibatch -output_grid_h=18 -output_grid_w=32
