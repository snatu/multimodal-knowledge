import socialiq_std_folds
import os
import json
import re
import subprocess

def make_json_for(vids, file_name):
    for vid in vids:
        vid_name = vid + "_trimmed-out.mp4"
        vid_length = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration', '-of', 'default=noprint_wrappers=1:nokey=1', os.path.join("raw/vision/raw", vid_name)])
        
        vid_filename = os.path.join("/work/sheryl/raw/qa_extra", vid + "_trimmed.txt")
        vid_file = open(vid_filename, "r")

        file_end = False

        while not file_end:
            line = vid_file.readline()
            if not line:
                vid_file.close()
                break
            if bool(re.match(r"q\d+:*(.)", line)):
                vid_dict = {}
                vid_dict["vid_name"] = vid
                vid_dict["ts"] = "0.00-" + vid_length.decode("utf-8").strip() # timestamp corresponding to question

                # question
                question_num = line.split(':')[0]
                str_list = line.split(':')[1:]
                question = ':'.join(str_list).strip()
                vid_dict['q'] = question
                vid_dict["qid"] = vid + "_" + question_num
                answer_num = 0
                while True:
                    pos = vid_file.tell()
                    next_line = vid_file.readline()
                    if not next_line:
                        vid_file.close()
                        file_end = True
                        break
                    if bool(re.match(r"a:*(.)", next_line)) or bool(re.match(r"i:*(.)", next_line)):
                        # answer
                        ans_str_list = next_line.split(':')[1:]
                        vid_dict["a"+str(answer_num)] = ':'.join(ans_str_list).strip()
                        answer_num += 1
                    else:
                        # question
                        file_name.write(json.dumps(vid_dict) + "\n")
                        vid_file.seek(pos)
                        break    

train_vids = socialiq_std_folds.standard_train_fold
val_vids = socialiq_std_folds.standard_valid_fold

all_vids = os.listdir("/work/sheryl/raw/vision/raw")

train_file = open("raw/siq_train.jsonl", "w")
val_file = open("raw/siq_val.jsonl", "w")

make_json_for(train_vids, train_file)
make_json_for(val_vids, val_file)