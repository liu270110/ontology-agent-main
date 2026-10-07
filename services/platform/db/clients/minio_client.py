"""MinIO 对象存储客户端薄封装（platform.db.clients「五存储客户端」占位的 minio 块填充）。

设计依据与边界：
- key 形态沿用既有契约：minio_key = "{bucket}/{object_path}"（首段=桶名），如
  ``raw-docs/{tenant}/{collection}/{doc}/source.pdf``——documents.minio_key 列既有形态
  （kb M2 JSON 通道仅登记 key 未落对象）；四桶由 deploy compose minio-init 幂等创建
  （raw-docs/extracts/ontology-artifacts/plugin-packages），本模块按 key 首段自动寻址；
- 同步 SDK（minio，Apache-2.0，主依赖）薄封装 + asyncio.to_thread 异步门面：调用方为
  FastAPI 异步路由 / 流水线异步步，SDK 网络调用禁阻塞事件循环；
- 桶缺失自动幂等创建（bucket_exists → make_buckets，实例级缓存防每次请求多打 HEAD）；
- S3Error 等存储异常原样上抛，错误语义归调用方映射（kb 路由 → 5004/503；流水线步 →
  步级重试 ≤3）；本模块不做重试、不做降级（无策略常量，D2 纪律）。
"""

from __future__ import annotations

import asyncio
import io
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 仅类型注解（minio 为运行时懒加载——保持模块可导入性，health.py 同款）
    from services.platform.config import Settings


def parse_minio_key(key: str) -> tuple[str, str]:
    """``{bucket}/{object_path}`` → (bucket, object_path)；形态非法抛 ValueError。"""
    bucket, _, object_path = key.partition("/")
    if not bucket or not object_path:
        raise ValueError(f"非法 minio_key（须为 {{bucket}}/{{object_path}} 形态）: {key}")
    return bucket, object_path


class MinioObjectStore:
    """对象存储门面：put_bytes / get_bytes 两个原子操作（异步）。

    直接接收已构建的 minio client（测试注入 fake 同款缝）；生产经 :meth:`from_settings`。
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._ensured_buckets: set[str] = set()

    @classmethod
    def from_settings(cls, settings: Settings) -> MinioObjectStore:
        """按平台唯一配置入口构建（OA_MINIO_ENDPOINT/USER/PASSWORD；lite 档 secure=False）。"""
        from minio import Minio

        return cls(
            Minio(
                settings.minio_endpoint,
                access_key=settings.minio_user,
                secret_key=settings.minio_password,
                secure=False,
            )
        )

    def _ensure_bucket(self, bucket: str) -> None:
        """桶存在性兜底（幂等；实例级缓存——compose minio-init 为常规创建路径，此处自愈）。"""
        if bucket in self._ensured_buckets:
            return
        if not self._client.bucket_exists(bucket):
            self._client.make_bucket(bucket)
        self._ensured_buckets.add(bucket)

    async def put_bytes(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        """写入对象（整体字节，v1 文件上限 50MB 量级不流式）；返回入参 key。"""
        bucket, object_path = parse_minio_key(key)

        def _put() -> None:
            self._ensure_bucket(bucket)
            self._client.put_object(bucket, object_path, io.BytesIO(data), length=len(data), content_type=content_type)

        await asyncio.to_thread(_put)
        return key

    async def get_bytes(self, key: str) -> bytes:
        """读取对象整体字节。"""
        bucket, object_path = parse_minio_key(key)

        def _get() -> bytes:
            response = self._client.get_object(bucket, object_path)
            try:
                return bytes(response.read())  # SDK 无类型标注（Any）→ 显式 bytes 收敛
            finally:
                response.close()
                response.release_conn()

        return await asyncio.to_thread(_get)

    async def delete_object(self, key: str) -> None:
        """删除对象（S3 语义幂等：对象不存在不报错）；存储异常原样上抛——尽力而为语义
        （失败留痕不阻断）由调用方兜（kb purge 同 put/get 的错误语义归调用方映射口径）。"""
        bucket, object_path = parse_minio_key(key)

        def _delete() -> None:
            self._client.remove_object(bucket, object_path)

        await asyncio.to_thread(_delete)
