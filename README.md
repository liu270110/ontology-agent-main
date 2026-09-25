# ontology-agent

基于本体（Ontology）知识的智能体项目（初始化阶段，代码陆续补充中）。

## 分支策略

| 分支 | 用途 | 说明 |
| ---- | ---- | ---- |
| `master` | 稳定分支 | 保持可发布状态，由 `develop` 验证通过后合并进入 |
| `develop` | 开发分支 | 日常开发分支，功能开发基于它拉出 feature 分支 |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行测试
pytest -v

# 运行程序
python main.py
```

## CI/CD（Gitee Go）

流水线配置文件：`.workflow/develop-pipeline.yml`

- **触发条件**：push 到 `develop` 或 `master` 分支
- **执行内容**：Python 3.9 构建 → 安装依赖 → 运行 pytest 测试
- **启用方式**：代码推送后在 Gitee 仓库页面进入「流水线」（Gitee Go），按提示开通并导入仓库中的 `.workflow` 配置即可

后续可在该文件中追加阶段：发布制品（`publish@artifact`）、构建 Docker 镜像、部署到服务器等。

## 项目结构

```
.
├── .workflow/           # Gitee Go 流水线配置
├── tests/               # 单元测试
├── main.py              # 程序入口
├── requirements.txt     # 依赖清单
└── README.md
```
