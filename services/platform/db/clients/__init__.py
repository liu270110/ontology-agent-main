"""五存储客户端：pg/neo4j/milvus/minio/redis（M3+ 填充）。

已填充：minio（:mod:`services.platform.db.clients.minio_client`，kb 文件直传通道 v1.5 wedge
首个生产消费方；gateway/health.py readyz 探活为既有的独立懒加载路径，不经此处）。
"""
