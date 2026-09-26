# dsec-sandbox-base：alpine + 出口管控/调度工具（沙箱最小基础镜像）
FROM alpine:3.20
RUN apk add --no-cache iptables iputils util-linux-misc curl ca-certificates tzdata python3 \
    && adduser -D -u 1000 dsecuser \
    && mkdir -p /var/log/chronus /var/dsec-protected /work \
    && chown dsecuser:dsecuser /var/log/chronus
COPY entry.sh /entry.sh
RUN chmod +x /entry.sh
WORKDIR /work
ENTRYPOINT ["/entry.sh"]
