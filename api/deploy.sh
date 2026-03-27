#!/bin/bash
set -e

echo "Building Lambda package for Python 3.12 (linux/amd64)..."
rm -rf package function.zip

docker run --rm \
  --platform linux/amd64 \
  --entrypoint /bin/bash \
  -v "$PWD":/var/task \
  -w /var/task \
  public.ecr.aws/lambda/python:3.12 \
  -c "pip install -r requirements.txt -t ./package --quiet"

echo "Zipping..."
cd package && zip -r ../function.zip . -x "*.pyc" -x "*/__pycache__/*" > /dev/null
cd ..
zip -r function.zip app/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null
zip -r function.zip ../migrations/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null
zip -r function.zip ../tests/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null
zip -r function.zip ../knowledge/ -x "*.pyc" -x "*/__pycache__/*" > /dev/null

export PATH="$PATH:/usr/local/bin:$HOME/.local/bin"
echo "Deploying..."
aws lambda update-function-code \
  --function-name rdmis-crm-api \
  --zip-file fileb://function.zip \
  --region us-east-1

echo "Cleaning up..."
rm -rf package function.zip

echo "Done."
