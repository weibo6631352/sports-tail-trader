# docs 目录说明

长期维护文档。先看 [../README.md](../README.md) 的"快速上手"，再按用途查下面这些文件。

## 怎么看

- 开发规则与分层边界：[../CLAUDE.md](../CLAUDE.md)
- 启动与页面操作：[使用说明书](./使用说明书.md)
- 市场 discovery 链路：[市场发现链路](./市场发现链路.md)
- Goalserve 数据接口：[goalserve.md](./goalserve.md)
- HTTP API：起服务后访问 `http://127.0.0.1:8000/docs`（Swagger）

策略配置 / runbook / 工作流文档不再单独维护——以 [CLAUDE.md](../CLAUDE.md) +
源码 + `git log` 为唯一权威。

## 维护

- 新增接口、配置项或人工操作入口时同步改文档。
- 名称和源码保持一致。
- 不写密钥、私钥、未脱敏响应。
- 不保留改造计划、迁移记录或被当前代码取代的历史描述。
- HTTP API 字段以 `/openapi.json` 为准，不在本目录手写。
