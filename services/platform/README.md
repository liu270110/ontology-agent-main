# platform（平台底座）

config 统一配置 / kernel(DomainError) / security(JWT+PBKDF2+authorize) / deps(请求级 Principal、UoW 注入) / errors(统一错误码与 GatewayError) / ports(ModelPort) / llm(模型网关) / db(Base+UoW+registry+Alembic+clients)。
