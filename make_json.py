import os
import json
import re
import subprocess
import sys

sys.path.append("/home/sheryl")

from raw import socialiq_std_folds

def make_json_for(vids, file_name):
    for vid in vids:
        vid_name = vid + "_trimmed-out.mp4"
        vid_length = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration', '-of', 'default=noprint_wrappers=1:nokey=1', os.path.join("raw/vision/raw", vid_name)])
        
        vid_filename = os.path.join("/home/sheryl/raw/qa", vid + "_trimmed.txt")
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
                correct_num = 0
                incorrect_num = 0
                while True:
                    pos = vid_file.tell()
                    next_line = vid_file.readline()
                    if not next_line:
                        vid_file.close()
                        file_end = True
                        break
                    prev_answers = vid_dict.values()
                    if bool(re.match(r"a:*(.)", next_line)):
                        # correct answer
                        ans_str_list = next_line.split(':')[1:]
                        answer = ':'.join(ans_str_list).strip()
                        if correct_num < 4 and answer not in prev_answers:
                            vid_dict["a"+str(answer_num)] = answer
                            answer_num += 1
                            correct_num += 1
                    elif bool(re.match(r"i:*(.)", next_line)):
                        # incorrect answer
                        ans_str_list = next_line.split(':')[1:]
                        answer = ':'.join(ans_str_list).strip()
                        if incorrect_num < 3 and answer not in prev_answers:
                            vid_dict["a"+str(answer_num)] = answer
                            answer_num += 1
                            incorrect_num += 1
                    else:
                        # question
                        file_name.write(json.dumps(vid_dict) + "\n")
                        vid_file.seek(pos)
                        break    

train_vids = socialiq_std_folds.standard_train_fold
val_vids = socialiq_std_folds.standard_valid_fold

all_vids = os.listdir("/home/sheryl/raw/vision/raw")

train_file = open("/home/sheryl/raw/siq_train.jsonl", "w")
val_file = open("/home/sheryl/raw/siq_val.jsonl", "w")

make_json_for(train_vids, train_file)
make_json_for(val_vids, val_file)