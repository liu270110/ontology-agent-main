"""知识库 graphrag-ontology 语义子包（OntRAG 知识库GraphRAG设计；代码落点锚点 §0）。

- chunking：步骤 1 文档预处理的语义分块（§2.1）；
- embed：嵌入客户端（Ollama bge-m3）+ BM25/向量召回 SQL（§4.0/§4.2）；
- retrieve：混合检索基线（向量+BM25 RRF k=60；§4.0/§4.2/§5）。
"""
