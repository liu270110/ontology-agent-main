FROM docker:27-dind

# edge agent 依赖：python3（标准库即可）+ 工具
RUN apk add --no-cache python3 erofs-utils fuse3 util-linux-misc curl \
    && adduser -D -u 1000 dsecuser

COPY edge_agent.py /opt/dsec/edge_agent.py
COPY entrypoint.sh /opt/dsec/entrypoint.sh
RUN chmod +x /opt/dsec/entrypoint.sh

# 覆盖镜像自带的 DOCKER_HOST=tcp://docker:2375（Docker Desktop 变体），使用本地 socket
ENV DOCKER_HOST=unix:///var/run/docker.sock

ENTRYPOINT ["/opt/dsec/entrypoint.sh"]
