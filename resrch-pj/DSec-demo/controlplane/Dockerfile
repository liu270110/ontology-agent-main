FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
COPY common.py iam.py watcher.py placement.py apiserver.py ./
EXPOSE 8000 8001 8002 8003
