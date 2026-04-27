# docs 目录说明

这里放长期维护文档。
先看 [../README.md](../README.md) 的“快速上手”，再按用途查下面这些文件。

## 怎么看

- 看仓库级开发规则和重构准则：看 [../AGENTS.md](../AGENTS.md)
- 看框架边界、分层和二次开发入口：看 [设计文档](./设计文档.md)
- 看市场 discovery 当前链路：看 [市场发现链路说明](./市场发现链路.md)
- 看运行配置：看 [config.md](./config.md)
- 看接口：看 [api.md](./api.md)
- 看运行与故障处理：看 [runbook.md](./runbook.md)

## 文档索引

- [../AGENTS.md](../AGENTS.md)：Codex rules、分层边界和结构改造准则
- [api.md](./api.md)：Admin API
- [config.md](./config.md)：配置说明
- [市场发现链路.md](./市场发现链路.md)：扩展 discovery hook、分页扫描、WS 热发现和运行时状态
- [设计文档.md](./设计文档.md)：框架边界、运行时结构、业务扩展契约、前端边界和手续费工具
- [runbook.md](./runbook.md)：故障处理

## 维护

- 新增接口、配置项或人工操作入口时，同步改文档。
- 名称和源码保持一致。
- 不写密钥、私钥、未脱敏响应。
- 不保留一次性执行清单、改造计划、迁移过程记录或已经被当前代码取代的历史表述。
