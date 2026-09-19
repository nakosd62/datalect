python3 stress_test.py \
--url https://ydyl-192590988505.us-east1.run.app \
--mode conversation \
--datasets-file conversation_datasets.json \
--prompts-file conversation_prompts.example.txt --prompt-order random \
--users 20 --duration 300 --interval 3 --turns-per-conversation 5 \
--model gpt-5.6-luna