# feishu-history

> A Hermes Agent plugin: fetch recent Feishu/Lark chat history on demand as context — a lurker-mode complement to `FEISHU_REQUIRE_MENTION`.

让 Agent 按需拉取飞书群聊历史，补全"潜水"期间缺失的上下文。

## 解决什么问题

`FEISHU_REQUIRE_MENTION=true` 时，群里没有 @bot 的消息会被直接丢弃（不进上下文）。
当用户说"接着刚才聊的"时，Agent 看不到"刚才"发生了什么。

本插件注册 `feishu_chat_history` 工具：Agent 发现上下文缺口时自主调用，
从飞书 API 拉取该群最近消息作为背景材料——按需补全，而不是全量囤积。

## 安装

- 放至 `~/.hermes/plugins/feishu-history/`（Hermes 插件目录），重新加载后生效
- 仅标准库、只读；复用网关的 `FEISHU_APP_ID`/`FEISHU_APP_SECRET`，需要 `im:message:readonly` 权限
- 无凭据时工具自动隐藏（fail closed）

## 工具签名

```
feishu_chat_history(chat_id?: str, count?: int, hours?: number)
```

- `chat_id`: oc_ 开头的会话 ID。**在飞书会话内调用时可省略**——自动从 gateway 会话上下文解析（contextvars → 环境变量，借鉴 arkseek/hermes-feishu 的设计）；非飞书平台的 chat_id 会被明确拒绝
- `count`: 拉取条数，1-50，默认 20
- `hours`: 时间窗，默认 24，最大 168（7 天）
- 返回 JSON：`{ok, chat_id, note, messages}`，note 提醒模型"仅作上下文，不要回应未 @ 自己的内容"

## 与 #47581 / #25728 的关系

上游 PR #47581（自动缓冲未提及消息）因安全边界和适配器迁移被要求 rework，
烂尾中。本插件走另一条路：不缓冲、不自动注入，Agent 按需主动拉取。
等上游功能落地后，本插件可直接删除，无任何残留。

## 设计约束（对齐 Hermes 插件契约）

- `kind: standalone`，放 `~/.hermes/plugins/`，`hermes update` 零冲突
- handler 永远返回 JSON 字符串（出错也是），接受 `**kwargs`
- `check_fn` 无飞书凭证时隐藏工具（fail closed）
- stdlib only（urllib），不导入笨重的 lark_oapi SDK
- 只读（`im/v1/messages` GET），无消息发送能力
- 凭证复用 feishu 平台适配器的 `FEISHU_APP_ID/SECRET`，不重复配置

## 需要的飞书权限

`im:message:readonly`（读取群历史）。若 403：开放平台 → 权限管理 → 申请并发布。

## 已验证

- 真实 API 拉取 ✓（tenant token、分页、时间过滤）
- 时间戳：毫秒字符串，µs 兼容
- body 兼容 dict/str 两种形态
- 错误路径：无效 chat_id / 缺权限 / 空窗口均返回可读错误
- 端到端：`hermes chat -q` 中模型成功发现并调用工具
- chat_id 解析链：显式参数 → gateway contextvars → 环境变量，三条路径均实测通过（含非飞书 id 拒绝）

## License

[MIT](LICENSE)
