# gateway（全局 HTTP 装配）

app 工厂 / 中间件链（RequestID→JWT→租户→限流→审计→异常）/ SSE 出口。只挂 `<模块>.api` 路由与 platform；业务规则一律在模块内。
