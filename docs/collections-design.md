# Zotero collection 路由设计（草案）

状态：**草案，尚未实现**。本文只描述设计，不改变任何行为。
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

## 4. 映射配置（草案）

```yaml
collections:
  # 总开关。AGENTS 要求分类质量先被验证，所以默认关闭。
  enabled: false

  # add-only：绝不删除不是本次加进去的成员。prune 打开后只清理"被管理的
  # collection"里的陈旧成员（见 §6）。
  prune: false

  # 把 inbox 变成真正的工作队列：路由后把论文移出 inbox。
  # 默认关闭，因为这是对用户既有组织的破坏性改动。
  remove_from_inbox: false

  # 每个维度独立开关。
  route:
    topics: true
    roles: false     # role 是facet（一篇可能 core+method+review），标签过滤比目录更合适
    review: true

  # 标签 -> 目标 collection。值可以是 8 位 key，也可以是名字或 "父/子" 路径；
  # 启动时解析一次，check 会打印解析结果。key 在改名后依然有效。
  routes:
    topic/loop-integrals:    YRM8M87H          # 02 Topics/Physics
    topic/amplitudes:        YRM8M87H          # 02 Topics/Physics
    topic/perturbative-qcd:  YRM8M87H          # 02 Topics/Physics
    topic/collider-phenomenology: YRM8M87H     # 02 Topics/Physics
    topic/nrqcd:             VIW97AJZ          # 02 Topics
    topic/quarkonium:        VIW97AJZ
    topic/pnrqcd:            VIW97AJZ
    topic/scet:              VIW97AJZ
    topic/heavy-flavor:      VIW97AJZ
    topic/fragmentation:     VIW97AJZ
    topic/energy-correlator: "02 Topics/Math"
    topic/experiment:        "02 Topics/Physics"

  review:
    # 所有 agent/review* 论文进这个队列（单队列，推荐）
    collection: "04 Review"
    # true = 每个 reason 一个 collection（agent/review/<reason> -> routes 里查）
    per_reason: false

  # 映射里没有的 collection 是否自动创建（放在这些父 collection 下）。
  # 第一版建议关闭：只用已存在的目标，少一条写路径。
  auto_create: false
  parents:
    topic: "02 Topics"
    role: "03 Roles"
    review: "04 Review"
```

字段语义小结：

| 字段 | 默认 | 作用 |
|---|---|---|
| `enabled` | `false` | 总开关，未验证前不动 collection |
| `prune` | `false` | 是否清理陈旧成员（仅限受管 collection） |
| `remove_from_inbox` | `false` | 是否把已路由论文移出 inbox |
| `route.topics/roles/review` | `true/false/true` | 各维度开关 |
| `routes` | `{}` | 显式映射，多对一允许 |
| `review.collection` / `per_reason` | 单队列 | review 队列形态 |
| `auto_create` / `parents` | `false` | 是否自动建 collection |

---

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

## 9. 分期实施

| 阶段 | 内容 | 是否改动 Zotero |
|---|---|---|
| P0 | `merge_collections()` 纯函数 + 映射解析与校验 + `check` 报告解析结果 + 测试 | 否 |
| P1 | `jevero route --dry-run`：打印"当前成员 → 计划成员"差集 | 否 |
| P2 | `jevero route --apply`：只做 add-only 的 topic 路由 | **是** |
| P3 | review 队列（单队列 / per_reason）+ 与 `process` 合并成一次 PATCH | **是** |
| P4 | `prune`、`remove_from_inbox`、`auto_create`（各自默认关闭） | **是** |
| P5 | role collection（若仍需要，见 §10） | **是** |

P0–P1 完全无风险，可以立刻做。

---

## 10. 待决定的问题

1. **inbox 要不要清空？** `remove_from_inbox: true` 会让 `00 Read NOW!!!` 变成真正的工作队列（处理完就移出）；`false` 则它永远是"所有处理过的论文"的副本。我倾向 **true**，但默认值先给 false，等你看过 dry-run 再决定。
2. **role 要不要 collection？** role 是 facet（一篇可能同时 `core`+`method`），目录会因此重复且不稳。我倾向**先不做**，用标签过滤。你如果需要，`route.roles: true` 即可开启。
3. **review 队列：一个还是按 reason 分？** 单队列（`04 Review`）最简单；按 reason 分（`04 Review/ambiguous`、`/taxonomy-gap`）更利于分别处理，但需要每类一个人工流程。你之前已经把标签拆成 state + reason，所以按 reason 分也有道理。
4. **要不要 auto_create？** 关闭时只有已存在的目录能被写入，最安全；开启后新 topic 会自动建目录，但可能在 `02 Topics/` 下产生与现有粗桶命名风格不一致的新目录（`02 Topics/perturbative-qcd` vs `02 Topics/Physics`）。
5. **`02 Topics/Physics` 这个粗桶怎么处理？** 上面草案把它作为 4 个 topic 的目标（多对一）。另一个选择是新建细目录（`02 Topics/Physics/<topic>`）保留你的粗桶层级——需要你定目录风格。
6. **是否需要在写入前人工确认？** 例如 `route --apply` 打印差集并要求 `--yes`。对 34 篇不必要，对全库批量重跑有用。

---

## 11. 与 AGENTS.md 的关系

本设计**不改变** AGENTS 的约束，只是把它实现出来：

- 模型仍然不决定任何持久化动作（路由吃的是标签）；
- 仍然不碰 SQLite，只用受支持的 API；
- 仍然"优先标签"，collection 是标签的投影；
- 仍然要求先验证分类质量（`enabled: false` 是这一条的技术体现）。
