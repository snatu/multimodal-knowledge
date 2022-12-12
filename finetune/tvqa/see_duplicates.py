with open('/data/train_results.txt', 'r') as f:
	count = 0
	frames = set()
	for line in f:
		frame = line.split("\t")[0]
		count += 1
		frames.add(frame)
	if 'hKfNp8NU82o_trimmed-out_104.jpg' in frames:
		print("IN THERE")
	print(count)
	print(len(frames))
