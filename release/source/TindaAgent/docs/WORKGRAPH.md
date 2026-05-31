# WorkGraph

WorkGraph 是 TindaAgent 的工作流可视化工作台，当前入口为 `/workgraph`。它还没有接入真实工作流事件源，页面内置一份样例 workflow，用于验证图布局、交互和视觉语言。

## 当前范围

- 页面文件：`TindaAgent/Web/workgraph.html`
- 服务端路由：`GET /workgraph`
- HOME 入口：登录后在 HOME 显示 WorkGraph 按钮
- 验证覆盖：`src/cli/httpFeatureSmoke.ts` 会检查 `/workgraph` 页面和核心渲染函数字符串
- 运行依赖：由 TypeScript Web server 作为静态 HTML 资产提供

## 数据模型

当前页面使用内置 `workflow` 对象，结构分为三部分：

- `actors`：工作流参与者，例如 client、router、planner、executor、tool、audit、guard、delivery
- `events`：按时间记录的工作流事件，包含 `actor_id`、`type`、`time`、`parent_id`、`summary`、`status`、`payload`
- `links`：事件之间的显式关系，包含 `from`、`to`、`type`

Actor Map 主要使用 `actors[].parent_id` 建立层级关系。Event Timeline 和 Lane Timeline 主要使用 `events[].parent_id` 与 `links` 建立事件关系。

## 视图

### Actor Map

Actor Map 是默认视图，展示 actor 的父子层级。当前布局是自上而下的阶级图：

- 每一层使用 `LEVEL n` 背景带区分深度
- 父子关系使用正交折线连接
- 同层 actor 会根据状态优先级和名称排序
- 节点居中排列，尽量适应当前画布宽度

### Event Timeline

Event Timeline 按事件时间顺序展示事件节点：

- 事件按 `time` 升序排列
- 节点上下错位，避免一条直线过密
- 中间有 `TIME ORDER` 轴线提示阅读方向
- 同时渲染 parent 关系和 `links` 关系

### Lane Timeline

Lane Timeline 按 actor 分泳道展示事件：

- actor 顺序按事件首次出现顺序生成
- 每个事件落到对应 actor 泳道
- 连线使用低存在感折线，降低跨泳道干扰
- 进入 Lane 视图时默认折叠底部详情区，把空间让给泳道

## 交互

### Hover 聚焦

鼠标悬浮节点时，WorkGraph 会进入临时 hover preview：

- 当前节点高亮
- 直接相关节点保持可见
- 不相关节点变暗
- 非相关连线变暗
- lane、level、axis 等图内背景元素一起压暗
- header、侧栏、状态图例、底部详情等图外控制区不变暗

如果已经存在左键选中态，hover preview 不会再触发，避免两个焦点模型叠加。

### 左键选中

当前实现里，左键点击节点会进入选中态：

- 被点击节点加 `.selected`
- 直接相关节点加 `.related`
- 不相关节点加 `.dimmed`
- 相关边加 `.selected-edge`
- 非相关边降低透明度
- 底部 `Selected` 和 `Raw Payload` 面板显示当前节点摘要和原始结构

再次点击同一节点，或点击画布空白区域，会清除选中态。

### 节点日志窗口

左键点击节点时，WorkGraph 会在画布上打开一个矩形浮窗。浮窗的几何中心尽量与被点击节点的几何中心对齐，并在靠近画布边界时自动夹紧，避免内容被裁掉。

窗口内容用于显示当前节点的日志上下文。当前版本还没有接入真实工具日志源，因此日志数据先从内置 `workflow.events` 派生，后续可以在 `nodeLogsFor(node)` 中替换为真实终端事件、工具运行时事件、LLM request metadata、审计日志或 WorkGraph event store。

窗口结构：

- 左侧：日志列表
- 右侧：选中日志详情或原始 payload
- 顶部：节点名称、状态灯、日志数量、节点设置按钮、关闭按钮

左侧日志列表每行字段：

- `[调用时间]`
- `[日志ID]`
- `[工具名称]`
- `[状态]`
- `[执行内容摘要]`

示例行：

```text
[22:02:10] [e_003] [codex-cli] [done] 生成 RenderScene 元素规范
[22:03:15] [e_004] [terminal] [active] 绘制 Actor Map 与事件节点
[22:04:00] [e_005] [status] [blocked] 等待真实事件源接入
```

窗口行为：

- 点击节点打开日志窗口
- 再次点击同一节点关闭窗口并清除选中态
- 点击其他节点时，窗口移动到新节点中心并切换日志源
- hover 聚焦仍然只负责临时视觉聚焦，不打开日志窗口
- 日志窗口打开后，图内 hover preview 不应干扰窗口阅读
- 点击窗口右上角关闭按钮，或点击画布空白区域，会关闭窗口并清除选中态
- 底部 `Selected` 和 `Raw Payload` 面板仍保留，用于调试结构化数据

### 节点设置界面预留

日志窗口右上角有节点设置按钮。点击后，同一个矩形窗口会切换到当前节点的设置界面骨架。

当前设置界面只做结构预留，不绑定真实保存逻辑，方便后续重新设计。已预留的稳定元素包括：

- `.node-settings-body`
- `.node-settings-nav`
- `.node-settings-tab`
- `.node-settings-content`
- `.node-settings-grid`
- `.node-setting-slot`
- `data-setting-slot="enabled"`
- `data-setting-slot="runtime"`
- `data-setting-slot="timeout"`
- `data-setting-slot="log-scope"`
- `data-setting-slot="tool-permissions"`
- `data-setting-slot="environment"`
- `data-setting-slot="status-light"`

预留设置内容：

- 节点启用
- 运行身份
- 执行超时
- 日志范围
- 工具权限
- 环境
- 状态灯阈值

设置界面目前有 `Back to logs` 操作，可切回日志视图。`Reset` 和 `Apply to node` 是预留按钮，不会持久化修改。

## 状态表达

节点状态使用圆形状态灯，不再显示文字 pill。文字状态仍保留在 `title` 和 `aria-label` 中。

当前状态颜色：

- `active` / `done`：绿灯
- `blocked` / `audit`：橙灯
- `failed`：红灯
- `pending` / `idle`：暗灯

右上角状态图例仍显示文字说明和计数，用于解释状态灯含义。

## 视觉语言

当前视觉方向是 Eva 初号机参考配色：

- 主体：紫黑底和紫色装甲感面板
- 强调：绿色状态灯、选中边、少量按钮点缀
- 警示：橙色用于 blocked/audit 和局部强调
- 失败：红色只用于 failed

绿色不作为大面积背景色使用，避免界面变成普通霓虹控制台。

## 实现要点

核心函数都在 `TindaAgent/Web/workgraph.html` 的内联脚本中：

- `layoutActorMap(data)`：生成 Actor Map 的 node/edge/lane scene
- `layoutEventTimeline(data)`：生成事件时间线 scene
- `layoutLaneTimeline(data)`：生成泳道时间线 scene
- `edgePath(edge, nodeMap)`：根据 edge layout 生成 SVG path
- `renderScene(scene)`：把 scene 渲染为 HTML 节点、SVG 边和背景层
- `nodeLogsFor(node)`：把当前样例 workflow 事件派生为节点日志
- `renderNodePanel(node, scene, mode)`：渲染节点日志/设置浮窗
- `renderNodeSettings(node)`：渲染当前节点设置界面预留骨架
- `highlightRelated(nodeId, active)`：hover preview 联动
- `applySelection(nodeId, scene)`：左键选中联动
- `clearSelection()`：清理选中和 hover 状态

渲染层分为三层：

- `laneLayer`：背景带、level、axis
- `edgeLayer`：SVG 连线和箭头 marker
- `nodeLayer`：可点击节点按钮

## 已知限制

- 当前 workflow 数据仍是页面内置样例，不是持久化事件源
- 没有工作流列表切换，左侧只显示当前样例 workflow
- 节点日志窗口的数据来自样例 workflow 事件，还不是实际工具调用日志
- 节点设置界面只是元素骨架，尚未接入真实配置保存
- 布局算法是前端本地计算，尚未抽成独立模块或测试单元
- Actor Map 在层级过深或同层 actor 很多时仍可能需要横向/纵向滚动

## 验证

当前最小验证：

```bash
npm run build
git diff --check
```

HTTP smoke 会覆盖 `/workgraph` 页面是否仍包含核心渲染能力：

```bash
npm run test:http-features
```
