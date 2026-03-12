#!/bin/bash

repo_dir=$(dirname $0)/..
/Users/timrozday/miniforge3/envs/dataharmonizer-dev/bin/python $repo_dir/scripts/submit_sample.py \
  --input $repo_dir/assets/test-fixtures/ERC000015_example.json \
  --linkml $repo_dir/schemas/ERC000015.yaml \
  --xsd $repo_dir/assets/ena_schema \
  --test \
  --force \
  --log $repo_dir/sample_submit_test.log \
  --output $repo_dir/sample_submit_test.out \
  --hold-until 2027-01-01
