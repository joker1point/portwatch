# portwatch Web 前端

React 19 + Vite + TypeScript。通过 SSE 订阅本机端口占用，delta 驱动局部更新。

## 运行

```bash
# 1) 先启动 API 层（在上一层目录）
cd ..
python server.py

# 2) 再启动前端
cd web
npm install
npm run dev      # → http://127.0.0.1:5173
```

API 地址默认 `http://127.0.0.1:8787`，可用环境变量覆盖：

```bash
# web/.env.local
VITE_API_BASE=http://127.0.0.1:8787
```

## 结构

```
src/
  types.ts              与 API 层对齐的类型定义
  api.ts                API 地址与 fetch 封装
  hooks/usePortStream.ts SSE 连接 + reducer 状态机（snapshot 灌全量，delta 局部更新）
  components/
    Header.tsx          品牌 + 采集心跳 + 统计读数
    FilterBar.tsx       搜索 / 协议过滤 / 权限过滤
    PortList.tsx        端口列表（进入动画、保护态标记）
    Timeline.tsx        变化时间线（added / removed / changed）
  styles/
    tokens.css          设计令牌（OKLCH 色彩、字体、间距、动效曲线）
    app.css             布局与组件样式
```

## 设计说明

方向是**工业仪器面板**：暖调深灰底 + 琥珀强调色，避免 `cyan-on-dark` 那套模板化配色。

- 端口号使用等宽字体 + `tabular-nums`，保证数字列对齐
- 行左缘的状态条标识可处置性：绿=正常、琥珀=归因受限、红=系统内核进程
- 顶部脉冲点跟随采集轮次跳动，让 2 秒轮询的"心跳"可被感知
- 全部动效仅使用 `transform` / `opacity`，并遵循 `prefers-reduced-motion`
