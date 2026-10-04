# services/seeds — 跨模块共享后端种子资产（唯一落位）

> 落位依据：332c179（`seeds/` 落位 `services/seeds/`——跨 kb/rsi/evals 共享后端运行资产随模块轴归位）。
> 本 README = 种子资产清单（**追加式维护**，standards/02 §6 共享文件纪律）。本体设计权威 =
> docs/ontology/本体核心设计.md（§4.1 IRI 与命名空间、§2.3 三路由、§5.2 SHACL 门禁）。

## 种子本体清单

| 种子资产 | 版本 | 引用形态（path@version） | 命名空间（前缀） | 范围 | 装载 / 对齐 / 门禁链路 |
| ---- | ---- | ---- | ---- | ---- | ---- |
| `power_seed.ttl` | v1 | `services/seeds/power_seed.ttl@v1` | `http://ontology-agent.local/o/t1/power#`（pw:） | 电力配电网停电分析（M2 出口条件，36 类 + R1/R2/R3 三路由全样例） | kb 七步流水线：`load_seed_catalog` / `match_seed_class`（services/kb/business/kb_extraction.py）+ `lint` / `validate`（services/ontology/core） |
| `drawing_seed.ttl` | v1 | `services/seeds/drawing_seed.ttl@v1` | `http://ontology-agent.local/o/t1/drawing#`（drw:） | A4 电力图纸本体 v0（G4 收窄：电气一次/二次图 + GB/T 制图规范；7 类 + 23 属性 + R001~R005 全 R2 SHACL 门禁） | 同款机制参数化装载：`load_seed_catalog(path)` / `match_seed_class` / `lint` / `validate`；对齐链路单测 tests/ontology/test_drawing_seed.py；C1 图纸摄取批次接入运行时 |
| （模块内）`services/ontology/seeds/power_outage_seed.ttl` | v0.2-m2-seed | 模块内装载（`seed_service.SEED_PATH`） | `https://ontology-agent.dev/ns/power#`（pwr:） | 电力停电分析精简 OB2（22 声明类，种子导入正式入口 `import_seed_as_project`） | services/ontology/business/seed_service.py（装载即 lint 自检门禁） |

## 纪律

- **版本化**：种子 Turtle 以 `owl:versionInfo` 头声明版本；一切消费方引用一律 `path@version` 形态
  （同 kb_extraction `_SHAPES_REF` 先例），禁止裸路径引用；改词表 = 升版本号，不就地改既有版本语义。
- **门禁即资产**：种子变更必须过同款 lint（三路由判定 / 术语唯一性）+ SHACL 自证；装载即自证
  （同 seed_service 装载语义——门禁不过视为资产损坏）。
- **脱敏**（standards/02 §11）：种子与样例只含通用领域术语与 SAMPLE-* 占位，零真实客户/项目标识。
- 关联资产：样例语料 [`samples/`](samples/power/MANIFEST.md)、抽取冒烟集
  [`sample_dataset/`](sample_dataset/README.md)、评估金标 [`golden/`](golden/README.md)。
