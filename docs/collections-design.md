# Zotero collection 路由设计（草案）

状态：**已实现**（`jevero route`）。本文记录最终设计；实现见 `src/jevero/routing.py`。
相关代码：`src/jevero/policy.py`（产出标签）、`src/jevero/zotero.py`（读写）、`config.yaml`（映射）。

对应 AGENTS.md 的 "Prefer tags before collection mutation"：collection 自动化要在分类质量被验证之后才加。目前只在 1 篇真实论文上验证过，所以本设计默认**关闭**。

---

## 1. 目标与非目标

**目标**：把已经写好的标签，确定性地投影成 Zotero collection 成员关系，让人可以按目录浏览，而不是只有标签面板。

```text
论文 → 标签（已实现） → 映射 → collection 成员（本设计）
```

**非目标**：

- 不让模型决定去哪个 collection（模型只给概率，路由只吃标签）。
- 不移动/删除论文，不碰附件、笔记、relations。
- 不改动任何"非本程序管理"的 collection 成员关系。

---

## 2. 核心设计原则

1. **路由是标签的投影，不是第二次判断。** 输入是 `policy.py` 已经产出的标签集合，不是概率。阈值只在一个地方解释（`policy.py`），否则同一个阈值会出现两种理解。
2. **显式映射，不按名字猜。** 见 §4：你的 `02 Topics/Physics` 是个粗桶，12 个精细 topic 里有 4 个都该进它；反过来 `topic/loop-integrals` 也可能没有对应 collection。所以映射必须是配置里写的一张表，不能靠"标签名 == collection 名"推断。
3. **add-only 是默认。** 一次运行绝不删除它不是自己加进去的成员关系。清理（prune）必须显式开启，且只作用于被本程序管理的 collection。
4. **与标签写入合并成一次 PATCH。** 避免"标签写成功、collection 写失败"的半完成状态。
5. **花钱的步骤和不花钱的步骤分开。** 路由不该需要重新调用模型。

---

## 3. 已验证的 API 事实（决定实现形态）

| 事实 | 影响 |
|---|---|
| 成员关系存在 **item 的 `collections` 数组**里，通过 `PATCH /items/<key>` 修改 | 不需要单独的"加入 collection"端点 |
| PATCH 的数组语义是**全量列表**："omitting a collection key would cause the item to be removed from that collection" | 必须**读→合并→写全量**，和 `merge_tags` 同样的纪律；否则会静默把论文踢出其它 collection |
| 创建 collection：`POST /collections`，body `[{name, parentCollection}]`；需要 `Zotero-Write-Token` 或 `If-Unmodified-Since-Version` | 创建与修改的前置条件不同（修改用 item version，创建用 write token） |
| Zotero 10+ 本地 API 支持 `POST`/`PATCH`/`DELETE` collections | 可以，但仅 10+ |
| collection 名可以重名、可被用户随时改；key 在库内稳定 | 配置存 key，注释写名字；名字作为可读别名也可接受但要解析后报告 |
| `GET /collections/<key>/items/top` **不含子 collection** | `jevero collections` 的计数已经是"直接包含"，路由也要按直接成员理解 |

你当前的结构（实测）：

```text
00 Read NOW!!!                    KRQJSKLH   <- inbox（已配置）
01 Projects                       CFD8CGK8
01 Projects/AmpNet                ITKHZCJR
01 Projects/GF                    MB9AEEKX
02 Topics                         VIW97AJZ
02 Topics/Finance                 2BL96Y65
02 Topics/Math                    WUGBYAMJ
02 Topics/Physics                 YRM8M87H
02 Topics/Physics/String Theory   2KRRI7FK
```

**注意 `02 Topics/Physics` 是粗桶**：`loop-integrals`、`amplitudes`、`perturbative-qcd`、`collider-phenomenology` 大概都该进它，而 `String Theory` 是个子桶——这直接说明映射必须是"多对一"，而不是"一对一"。

---

## 4. 配置（最终形态）

按 tag 直接路由，不再需要手写映射表：**标签名就是目录名**。

```yaml
collections:
  topics_parent: "02 Topics"      # topic/<name> -> 02 Topics/<name>
  roles_parent: "03 Roles"        # role/<name>  -> 03 Roles/<name>
  review_collection: "04 Review"  # 所有 agent/review* 进同一个队列
  route_roles: true
  remove_from_inbox: false
```

| 字段 | 默认 | 作用 |
|---|---|---|
| `topics_parent` | `02 Topics` | `topic/<name>` 的目标父目录 |
| `roles_parent` | `03 Roles` | `role/<name>` 的目标父目录 |
| `review_collection` | `04 Review` | 所有 `agent/review*` 的单队列；留空则不作队列 |
| `route_roles` | `true` | 是否给 role 建目录（关掉就只用标签） |
| `remove_from_inbox` | `false` | 路由后是否把论文移出 inbox |

要点：

- **标签名 = 目录名**，所以"粗桶还是细目录"这个问题消失了：`topic/loop-integrals` 就是
  `02 Topics/loop-integrals`。已有的粗桶（`02 Topics/Physics`）不再被使用，可以自行删除或改名。
- **自动创建**：缺失的父目录和子目录都会创建（`02 Topics` → `02 Topics/loop-integrals`），
  子目录挂在正确的父目录下。
- **review 单队列**：四个 reason 都进 `04 Review`——"需要人看"这件事是一样的；
  标签仍然区分 reason，所以队列内部可以按标签过滤。
- **role 目录默认开启**（最初需求包含 role），`route_roles: false` 一行即可关闭。
- 没有 `enabled` 开关、没有 `prune`、没有 `--yes`：命令本身是显式的，且默认 dry-run。

## 5. 动作语义（确定性）

新增一个纯函数层（`routing.py`），接口刻意保持与 `policy.py` 同构：

```python
class CollectionActions(BaseModel):
    add: set[str] = set()      # collection keys
    remove: set[str] = set()   # collection keys

def plan_membership(
    tag_actions: PolicyActions,     # policy.plan() 的输出，不是概率
    config: Config,
    current: set[str],              # 该 item 当前的 collection keys
) -> CollectionActions
```

判定逻辑（全部可追溯到配置）：

```text
如果 collections.enabled 且 agent/processed ∈ tag_actions:
    对 tags.add_tags 里每个受管标签（在 routes 中且对应维度开关打开）:
        target = 解析后的 key
        if target ∉ current: add(target)

    if agent/review ∈ tag_actions 且 route.review:
        add(review.collection)          # 单队列
        或 add(routes[f"agent/review/{reason}"])   # per_reason

    if prune:
        for key in current ∩ 受管目标集合:
            if 该 key 不再是本次应属的目标: remove(key)

    if remove_from_inbox and add 非空:
        remove(inbox_key)

    如果 add ∪ remove 为空: 不做任何请求（幂等）
```

要点：

- **`agent/error` 不路由**（失败论文没有可投影的语义）；`agent/processed` 本身也不进 collection——它是状态，不是位置。
- review 论文若同时有 topic，则**同时**进 topic collection 和 review 队列（目录是归档，review 是工作队列）。
- `missing-abstract` 这类没有 topic 的论文只进 review 队列。
- 一次 `PATCH` 同时带 `tags` 和 `collections`（两个字段都是全量列表）。

---

## 6. 安全规则

| 规则 | 理由 |
|---|---|
| 只改 item 的 `tags` 和 `collections` 两个字段 | PATCH 未提交的字段服务端不动 |
| `collections` 必须读后合并写全量 | 数组是全量语义，漏写会静默踢出成员 |
| 默认只加不删；prune 仅作用于 `routes` 指向的目标 + review 队列 | 不破坏用户手工整理的其他目录 |
| `remove_from_inbox` 默认关闭 | 把论文移出收件箱是用户可见的破坏性动作 |
| 目标的 key 解析失败 → 该篇**跳过路由并报告**，不失败整篇 | 分类结果和标签仍然有效，不该被一个目录配置拖垮 |
| 自动创建去重：一次运行内缓存已建 key；创建需要 write token | 避免同名重复创建 |
| 同一输入 → 同一成员计划 | 可测试、可审计 |
| `--dry-run` 是默认，且打印"当前成员 → 计划成员"的差集 | 与人已有的组织对照 |

---

## 7. 流程与 CLI

**关键：把花钱的和不花钱的分开。**

```bash
jevero process --dry-run          # 调模型，只打印计划标签（花钱）
jevero process --apply            # 调模型 + 写标签（花钱）

jevero route --dry-run            # 只读标签，打印成员计划（免费、离线、可反复跑）
jevero route --apply              # 只改 collection 成员（免费）
```

- `route` **完全不调用模型**，只读 item 的 tags 和 collections。所以改映射表后可以零成本重跑全部论文。
- `process --apply` 是否顺带路由，由 `collections.enabled` 决定（打开后一次 PATCH 同时写标签和成员）。
- `jevero check` 增加一节：把 `routes` 里每个值解析成 key 并打印，解析失败/歧义/`enabled: true` 但未验证都明确报警。这能在写之前抓到配置错误。

---

## 8. 存量论文的迁移

你库里 34 篇未处理论文的路径是：

```text
jevero process --apply            # 打标签（唯一花钱的一步，~$0.004）
jevero route --dry-run            # 看成员计划，对照你现有目录
jevero route --apply              # 落地
```

因为 `route` 只读标签，调映射表不会产生任何额外模型成本。

---

## 9. 实施状态

| 内容 | 位置 |
|---|---|
| `target_paths()`：标签 → 目标路径（纯函数） | `routing.py` |
| `merge_collections()`：保留非受管成员 | `zotero.py` |
| `ensure_collection_path()`：解析 + 按需创建（运行内缓存） | `zotero.py` |
| `apply_membership()`：PATCH `collections` 全量列表 | `zotero.py` |
| `jevero route --dry-run / --apply` | `main.py` |

测试覆盖：标签→路径的各种组合、成员合并、创建与缓存、dry-run 零写入、
未分类论文被跳过。

**未做**（也不打算做）：prune（清理陈旧成员）、按 reason 拆分队列、写前人工确认。
需要时再加，都不影响现有数据结构。

---

## 10. 已决定的取舍

| 问题 | 决定 |
|---|---|
| 已有 collection 怎么办 | 不管也不改；按 tag 直接路由，旧粗桶可自行删除 |
| 手写映射表 | 不要；标签名即目录名 |
| topic 目录风格 | 按 tag，不用粗桶 |
| role 要不要目录 | 要（`route_roles: true`），可一行关闭 |
| review 队列 | 单队列 `04 Review` |
| 自动创建 | 要 |
| CLI | 简洁：`route` 默认 dry-run，`--apply` 才写 |

## 11. 与 AGENTS.md 的关系

本设计**不改变** AGENTS 的约束，只是把它实现出来：

- 模型仍然不决定任何持久化动作（路由吃的是标签）；
- 仍然不碰 SQLite，只用受支持的 API；
- 仍然"优先标签"，collection 是标签的投影；
- 仍然要求先验证分类质量（`enabled: false` 是这一条的技术体现）。
