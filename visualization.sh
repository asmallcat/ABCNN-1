#!/bin/bash

python src/visualization.py \
    --config_path bcnn_config.json \
    --checkpoint_path checkpoints/quora/word2vec/google_news/bcnn/best_checkpoint \
    --examples_path data/hulo/hulo_demo_examples.csv \
    --output_dir plots/hulo/word2vec/google_news/bcnn/hulo_demo_examples
