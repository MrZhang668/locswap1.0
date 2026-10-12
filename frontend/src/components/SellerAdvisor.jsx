import { useEffect, useRef, useState } from 'react'
import { api, compressImage } from '../api'

const SUGGESTS = [
  '附近有人在找我挂出来的东西吗？',
  '我哪件商品的意向买家最多？',
  '我的定价合理吗？想卖快一点该怎么定？',
  '我想出个东西——这是照片',
]

const CONDITIONS = ['全新', '几乎全新', '成色很好', '成色良好', '成色一般']

// Mirrors publishing/schemas.py FIELD_LABELS_ZH: missing_fields carries raw
// field names, and a seller should not have to read an API to know what is
// missing.
const FIELD_LABEL = {
  name: '标题', summary: '一句话摘要', description: '描述', category: '类别',
  brand: '品牌', condition: '成色', color: '颜色', material: '材质',
  size: '尺寸规格', price: '价格', photo: '照片',
}

const CONFIDENCE_LABEL = { high: '高置信', medium: '中等置信', low: '低置信', none: '无样本' }

const DISCLAIMER =
  '顾问只读取需求与成交数据并给出建议：它不会改价格、不会编辑或下架你的商品，' +
  '也不会自作主张联系买家——只有你点了卡片上的按钮，商品才会被发出去。' +
  '所有买家需求都是匿名展示的。'

/** Which tools actually ran, in plain business language.
 *
 * Rendered from the backend's `used_tools` — a record of what executed — rather
 * than from anything the model says about its own sources. A model listing its
 * sources can hallucinate them; an execution log cannot.
 */
const SOURCE_LABEL = {
  list_my_items: '我的在售商品',
  find_potential_buyers: '买家求购',
  analyze_demand: '需求走势',
  analyze_sold_prices: '成交价记录',
}

const ROUTE_LABEL = {
  sales_agent: '销售分析',
  publishing_agent: '上架助手',
  status_change: '上下架',
  defer_sale: '出售计划',
  clarify: '需要确认',
  item_not_found: '需要先上架',
  listing_done: '已上架',
}

// Call-site ids as the backend logs them. Shown in the breakdown so a turn
// that cost more than expected can be explained by *which* step spent it —
// a routing call and a four-round tool loop are very different bills.
const CALL_SITE_LABEL = {
  'D.orchestrator_router': '意图路由 / 行动提议',
  'C.seller_advisor': '销售分析',
  'E.publishing_compliance': '发布合规检查',
  'F.publishing_extract': '商品信息提取',
}

const fmtTokens = (n) => (n >= 10000 ? `${(n / 1000).toFixed(1)}k` : String(n))

/** What this one question cost, per message.
 *
 * The collapsed line is the whole point — a single number the seller can
 * ignore. The breakdown is behind a click because it is diagnostic: nobody
 * needs four call-site rows under every answer, but when one turn costs five
 * times the last one, the reason is right there.
 *
 * `calls` is rendered even when the totals are zero: zero tokens across zero
 * calls means nothing ran (degraded, or no listings to analyze), while zero
 * across three calls means the provider stopped reporting usage. Collapsing
 * both into a bare "0" would hide the second.
 */
function TokenBadge({ usage }) {
  const [open, setOpen] = useState(false)
  if (!usage?.calls) return null
  const total = (usage.input || 0) + (usage.output || 0)
  const rows = Object.entries(usage.detail || {})
    .sort((a, b) => (b[1].input + b[1].output) - (a[1].input + a[1].output))
  return (
    <div className="token-badge">
      <button type="button" className="token-toggle" onClick={() => setOpen(!open)}
              title="本轮问答消耗的 token——点击查看分项">
        ⛃ {fmtTokens(total)} tokens
        <span className="token-split">
          （输入 {fmtTokens(usage.input || 0)} · 输出 {fmtTokens(usage.output || 0)}
          {' '}· {usage.calls} 次模型调用）
        </span>
      </button>
      {open && (
        <div className="token-detail">
          {rows.length === 0 && <div>没有分项数据。</div>}
          {rows.map(([site, v]) => (
            <div key={site} className="token-row">
              <span>{CALL_SITE_LABEL[site] || site}</span>
              <span>
                {fmtTokens(v.input)} + {fmtTokens(v.output)}
                {v.calls > 1 ? ` · ${v.calls} 次` : ''}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// 一条出售计划的卡片。
//
// 它存在的理由有两个，都和「不要让卖家去信任一句话」有关：
//
// 1. 把系统**推断出来的日期**摆出来。「12 月放假前」被换算成的那一天，和卖家
//    亲口说的那一天，在一段文字里长得一模一样——只有单独列出来，错的那个才有
//    机会被改掉。
// 2. 把 `watching_now` 摆出来。计划活着、这一条开着、总开关开着、已经进了观察
//    窗口——四个条件都成立才真的在盯。少一个，卖家看到的「已排好」就是一句空话。
function SalePlanCard({ plan, onToggle, onCancel, busy }) {
  if (!plan) return null
  const inferred = plan.date_basis && !['explicit_date', 'manual'].includes(plan.date_basis)
  const state = plan.status !== 'active'
    ? { text: plan.status === 'cancelled' ? '已撤销' : '已结束', tone: 'muted' }
    : plan.watching_now
      ? { text: '正在每周盯着', tone: 'ok' }
      : !plan.watch_enabled
        ? { text: '这条已暂停', tone: 'warn' }
        : { text: `还没开始盯（${plan.watch_from} 起）`, tone: 'muted' }

  return (
    <div className="card" style={{ padding: 12 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
        <div style={{ fontWeight: 600 }}>出售计划 · {plan.item_name}</div>
        <span className={`mini ${state.tone === 'ok' ? '' : 'muted'}`}>{state.text}</span>
      </div>
      <div className="mini">
        <div>开卖日期：{plan.target_date}{inferred ? '（我推断的，不对就告诉我具体日期）' : ''}</div>
        <div>盯行情：{plan.watch_from} 起，每周一看一次</div>
        {plan.category
          ? <div>参照类别：{plan.category}</div>
          : <div className="muted">没归进类别，只按名字匹配搜索措辞——预订那条判据用不上</div>}
        {plan.hold_item && (
          <div>
            商品已收起（在你库里，买家搜不到）——在「我的橱窗」恢复上架即可开卖，
            <b>恢复后这条计划作废</b>
          </div>
        )}
        {plan.notified_count > 0 && <div>已经提醒过 {plan.notified_count} 次</div>}
      </div>
      {plan.status === 'active' && (onToggle || onCancel) && (
        <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
          {onToggle && (
            <button className="btn-ghost mini" disabled={busy}
                    onClick={() => onToggle(plan.plan_id, !plan.watch_enabled)}>
              {plan.watch_enabled ? '暂停这条' : '继续盯这条'}
            </button>
          )}
          {onCancel && (
            <button className="btn-ghost mini" disabled={busy}
                    onClick={() => onCancel(plan.plan_id, plan.item_name)}>撤销</button>
          )}
        </div>
      )}
    </div>
  )
}

// 出售计划监控的总开关。
//
// 它和对话里的 `set_plan_watch` 工具改的是**同一个字段**，所以卖家说一句「把提醒
// 都关了」之后，这个按钮会跟着灭——每一轮回复都带回 `watch_enabled`，界面照着它走。
// 两边各存一份状态，就会出现按钮亮着而实际不发信，而那个亮着的按钮比没有按钮更糟。
function WatchSwitch({ state, busy, onToggle }) {
  if (!state) return null
  const on = state.enabled
  return (
    <div className="advisor-watch">
      <button className={`watch-toggle${on ? ' is-on' : ''}`} disabled={busy}
              aria-pressed={on}
              title={on ? '关掉之后计划都保留，只是不再提醒' : '打开之后按各条计划的日期继续盯'}
              onClick={() => onToggle(!on)}>
        <span className="watch-dot" />
        出售计划监控 · {on ? '开' : '关'}
      </button>
      <span className="mini muted">
        {state.active_plans === 0
          ? '还没有计划——说一句「转换器我 12 月再卖」就排上了'
          : on
            ? `${state.active_plans} 条计划，其中 ${state.watching_now} 条正在盯`
            : `${state.active_plans} 条计划都还在，但现在一条都不会提醒你`}
      </span>
    </div>
  )
}

// `peek_plan_demand` 的结果。
//
// `would_notify` 直接照后端给的话显示，不在前端重新算一遍阈值——两处各算一次，
// 迟早会在某个边界上给出不同的答案，而卖家看到的是哪一个完全取决于他在看哪块屏。
function PlanDemandCard({ data }) {
  if (!data) return null
  const c = data.current
  return (
    <div className="card" style={{ padding: 12 }}>
      <div style={{ fontWeight: 600, marginBottom: 6 }}>{data.item_name} · 现在的行情</div>
      {c && (
        <div className="mini">
          <div>这一周（{c.week_start} 起）：{c.people} 人在搜，其中 {c.matching_people} 人的措辞和你这件对得上</div>
          <div>预订 {c.reserve_count} 单 · 搜索 {c.events} 次{c.trend ? ` · 趋势 ${c.trend}` : ''}</div>
        </div>
      )}
      {data.would_notify?.length > 0 && (
        <ul className="mini" style={{ margin: '6px 0 0', paddingLeft: 18 }}>
          {data.would_notify.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
      {data.caveats?.length > 0 && (
        <div className="mini muted" style={{ marginTop: 6 }}>{data.caveats.join(' ')}</div>
      )}
    </div>
  )
}

function SourceBadges({ tools, route, usage }) {
  const parts = []
  if (route && ROUTE_LABEL[route]) parts.push(`由${ROUTE_LABEL[route]}处理`)
  if (tools?.length) parts.push(`数据来源：${tools.map((t) => SOURCE_LABEL[t] || t).join(' · ')}`)
  if (!parts.length && !usage?.calls) return null
  return (
    <div className="mini muted" style={{ marginTop: 6 }}>
      {parts.join(' — ')}
      <TokenBadge usage={usage} />
    </div>
  )
}

/** Anonymized potential-buyer cards (fields = the backend whitelist; the frontend stores no internal buyer identifiers).
 *
 * The push button sends `card_id` — the ordinal shown on screen — plus the
 * item_id the backend tagged this result with. Card ordinals only mean anything
 * inside one listing's candidate list, so the id has to travel with the cards
 * rather than come from anything the user last clicked.
 */
/** An empty analysis result, shown as a conclusion rather than as a blank card.
 *
 * The three parts come from the backend's `EmptyResult` and are shown in the
 * order they have to be read: what was queried, what came back, what to do
 * next. A card that says only "没有数据" is unfalsifiable — the seller cannot
 * tell whether it means "not in the last 7 days" or "not ever", and those call
 * for different next moves.
 *
 * `fallback` covers a result produced before this field existed (a replayed
 * transcript, an older client); the copy is the same sentence the card used to
 * show on its own.
 */
function EmptyNote({ empty, fallback }) {
  if (!empty) return <div className="mini muted" style={{ marginTop: 6 }}>{fallback}</div>
  return (
    <div className="mini" style={{ marginTop: 6 }}>
      <div className="muted">查询口径：{empty.scope}</div>
      <div style={{ marginTop: 2 }}>{empty.message}</div>
      {empty.alternatives?.length > 0 && (
        <ul style={{ margin: '4px 0 0', paddingLeft: 16 }} className="muted">
          {empty.alternatives.map((a) => <li key={a}>{a}</li>)}
        </ul>
      )}
    </div>
  )
}

function BuyerCards({ data }) {
  const [pushing, setPushing] = useState(null)
  const [sent, setSent] = useState({})       // card_id -> confirmation copy
  const [failed, setFailed] = useState({})   // card_id -> error copy

  if (!data) return null
  const itemId = data.item_id

  const push = async (cardId) => {
    setPushing(cardId)
    setFailed((prev) => ({ ...prev, [cardId]: null }))
    try {
      const res = await api.pushToBuyer(itemId, cardId)
      setSent((prev) => ({ ...prev, [cardId]: res.message || 'Sent.' }))
    } catch (e) {
      setFailed((prev) => ({ ...prev, [cardId]: e.message }))
    } finally {
      setPushing(null)
    }
  }

  return (
    <div className="card">
      <div className="tiny strong" style={{ marginBottom: 6 }}>
        潜在买家需求 · {data.item_name} · 共 {data.total} 条
      </div>
      {data.cards.length === 0 && (
        <EmptyNote empty={data.empty} fallback="当前没有匹配的买家求购。" />
      )}
      {data.cards.map((c) => (
        <div key={c.card_id} style={{ borderTop: '1px solid var(--line)', padding: '8px 0' }}>
          <div style={{ fontSize: 13.5 }}>{c.demand_summary}</div>
          <div className="mini muted" style={{ marginTop: 3 }}>
            {c.budget_display} · {c.distance_display} · {c.freshness}
          </div>
          <div className="mini muted" style={{ marginTop: 2 }}>
            匹配度 {(c.match_score * 100).toFixed(0)}%：{c.match_reasons.join('、')}
          </div>
          {sent[c.card_id] ? (
            <div className="mini" style={{ marginTop: 6, color: 'var(--ok, #2a7)' }}>
              ✓ {sent[c.card_id]}
            </div>
          ) : (
            <button className="btn ghost sm" style={{ marginTop: 6 }}
                    disabled={!itemId || pushing === c.card_id}
                    onClick={() => push(c.card_id)}>
              {pushing === c.card_id ? '发送中…' : '📨 把我的商品发给这位买家'}
            </button>
          )}
          {failed[c.card_id] && (
            <div className="mini" style={{ marginTop: 4, color: 'var(--err, #c33)' }}>
              {failed[c.card_id]}
            </div>
          )}
        </div>
      ))}
      {data.caveats?.length > 0 && (
        <div className="mini muted" style={{ marginTop: 6 }}>⚠ {data.caveats.join('; ')}</div>
      )}
    </div>
  )
}

/** Sold-price distribution for one listing.
 *
 * The counterpart of DemandChart, and inline SVG for the same reasons. It
 * exists because without it the model had the whole distribution and no way to
 * hand back a picture of it, so when a seller asked for a chart it drew one out
 * of box-drawing characters — right numbers, unusable output.
 *
 * Three things share one axis, because the question "is my price sensible" is
 * exactly the relationship between them: the bars are how many items sold in
 * each price band, the box under them is the quartile spread, and the marker is
 * where this seller's own asking price falls.
 */
function PriceDistributionChart({ dist }) {
  if (!dist?.bins?.length) return null
  const W = 420
  const H = 138
  const PAD_L = 24
  const PAD_R = 10
  const PAD_T = 8
  const BOX_H = 26            // strip under the bars, for the box plot
  const PAD_B = 16 + BOX_H
  const innerW = W - PAD_L - PAD_R
  const innerH = H - PAD_B - PAD_T
  const maxCount = Math.max(1, ...dist.bins.map((b) => b.count))

  // The axis spans the data *and* the asking price: a price far above every
  // sale is the single most useful thing this chart can show, and clipping it
  // to the bars' range would hide exactly that case.
  const lo = Math.min(dist.bins[0].lo, dist.current_price)
  const hi = Math.max(dist.bins[dist.bins.length - 1].hi, dist.current_price)
  const span = hi - lo || 1
  const x = (v) => PAD_L + ((v - lo) / span) * innerW
  const y = (c) => PAD_T + innerH - (c / maxCount) * innerH
  const boxTop = PAD_T + innerH + 8
  const b = dist.box

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      role="img"
      aria-label={`Sold-price distribution over ${dist.sample_size} sales, with this listing's asking price marked`}
      style={{ display: 'block', marginTop: 8, overflow: 'visible' }}
    >
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line x1={PAD_L} x2={W - PAD_R} y1={y(maxCount * f)} y2={y(maxCount * f)}
                stroke="var(--line)" strokeWidth="1" />
          <text x={0} y={y(maxCount * f) + 3} fontSize="9" fill="var(--ink-3)">
            {Math.round(maxCount * f)}
          </text>
        </g>
      ))}

      {dist.bins.map((bin) => {
        const x0 = x(bin.lo)
        const w = Math.max(2, x(bin.hi) - x0 - 2)
        return (
          <rect key={`${bin.lo}-${bin.hi}`}
                x={x0 + 1} y={y(bin.count)} width={w}
                height={Math.max(0, PAD_T + innerH - y(bin.count))}
                rx="2" fill="var(--brand-soft)" stroke="var(--brand)" strokeWidth="0.75">
            <title>{`${bin.label}：${bin.count} 件成交`}</title>
          </rect>
        )
      })}

      {/* Box plot: whiskers to min/max, box across the interquartile range,
          heavy line at the median. */}
      <line x1={x(b.min_price)} x2={x(b.max_price)} y1={boxTop + BOX_H / 2} y2={boxTop + BOX_H / 2}
            stroke="var(--ink-3)" strokeWidth="1" />
      {[b.min_price, b.max_price].map((v, i) => (
        <line key={i} x1={x(v)} x2={x(v)} y1={boxTop + 4} y2={boxTop + BOX_H - 4}
              stroke="var(--ink-3)" strokeWidth="1" />
      ))}
      <rect x={x(b.p25)} y={boxTop + 2} width={Math.max(1, x(b.p75) - x(b.p25))}
            height={BOX_H - 4} rx="2"
            fill="var(--brand-soft)" stroke="var(--brand)" strokeWidth="1">
        <title>{`中间一半的成交价：$${b.p25} – $${b.p75}`}</title>
      </rect>
      <line x1={x(b.median)} x2={x(b.median)} y1={boxTop + 2} y2={boxTop + BOX_H - 2}
            stroke="var(--brand-deep)" strokeWidth="2">
        <title>{`中位数 $${b.median}`}</title>
      </line>

      {/* The seller's own price, drawn last so it is never hidden by a bar. */}
      <line x1={x(dist.current_price)} x2={x(dist.current_price)}
            y1={PAD_T} y2={boxTop + BOX_H}
            stroke="var(--warn-ink, #c80)" strokeWidth="1.5" strokeDasharray="3 2">
        <title>{`你的标价 $${dist.current_price}`}</title>
      </line>
      <text x={Math.min(W - PAD_R, Math.max(PAD_L, x(dist.current_price)))} y={H - 2}
            fontSize="8.5" textAnchor="middle" fill="var(--warn-ink, #c80)">
        你的标价 ${dist.current_price}
      </text>

      <text x={PAD_L} y={boxTop - 1} fontSize="8.5" fill="var(--ink-3)">${b.min_price}</text>
      <text x={W - PAD_R} y={boxTop - 1} fontSize="8.5" textAnchor="end" fill="var(--ink-3)">
        ${b.max_price}
      </text>
    </svg>
  )
}

/** Several listings on one fixed set of metrics.
 *
 * Rendered from `comparison`, which the backend assembles from the tool
 * results — not from anything the model wrote. That is the point: the promise
 * this table makes is that every listing is described on the same axes in the
 * same order, and a promise that depends on the model remembering it each turn
 * is not one. Cells reading 「无数据」 arrive that way from the server.
 */
function ComparisonCard({ data }) {
  if (!data?.rows?.length) return null
  return (
    <div className="card">
      <div className="tiny strong" style={{ marginBottom: 6 }}>
        {data.title} · {data.rows.length} 件商品
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className="mini" style={{ borderCollapse: 'collapse', width: '100%' }}>
          <thead>
            <tr>
              <th style={TH}>商品</th>
              {data.columns.map((c) => <th key={c} style={TH}>{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((r) => (
              <tr key={r.item_id}>
                <td style={{ ...TD, fontWeight: 600 }}>{r.item_name}</td>
                {r.cells.map((cell, i) => (
                  <td key={data.columns[i] ?? i}
                      style={{ ...TD, color: cell === '无数据' ? 'var(--ink-3)' : 'var(--ink)' }}>
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.caveats?.length > 0 && (
        <div className="mini muted" style={{ marginTop: 6 }}>⚠ {data.caveats.join('; ')}</div>
      )}
    </div>
  )
}

const TH = {
  textAlign: 'left', padding: '4px 8px', whiteSpace: 'nowrap',
  borderBottom: '1px solid var(--line)', color: 'var(--ink-3)', fontWeight: 600,
}
const TD = {
  textAlign: 'left', padding: '4px 8px', whiteSpace: 'nowrap',
  borderBottom: '1px solid var(--line)',
}

/** Sold-price statistics (from a tool, verifiable) + three-tier suggestions (model advice, no guarantee of a sale). */
function PricingCard({ data }) {
  if (!data) return null
  const s = data.stats
  return (
    <div className="card">
      <div className="tiny strong" style={{ marginBottom: 6 }}>
        成交价证据 · {data.item_name} · {s.sample_size} 条样本 · {CONFIDENCE_LABEL[data.confidence] ?? data.confidence}
      </div>
      {s.sample_size > 0 ? (
        <>
          <div className="mini muted">成交时间范围：{s.time_range}</div>
          <div className="row" style={{ gap: 6, marginTop: 6, flexWrap: 'wrap' }}>
            {[['最低', s.min_price], ['P25', s.p25], ['中位数', s.median], ['P75', s.p75], ['最高', s.max_price]]
              .map(([label, v]) => (
                <div key={label} className="mini" style={{ background: 'var(--brand-soft)', borderRadius: 8, padding: '4px 8px' }}>
                  {label} ${v}
                </div>
              ))}
          </div>
          {data.samples?.length > 0 && (
            <div className="mini muted" style={{ marginTop: 6 }}>
              样本：{data.samples.map((x) => `${x.name} $${x.sold_price}（${x.sold_on}）`).join('；')}
            </div>
          )}
          <PriceDistributionChart dist={data.distribution} />
        </>
      ) : (
        <EmptyNote empty={data.empty}
                   fallback="平台上还没有这类商品的有效成交价证据。" />
      )}
      {data.suggestions?.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {data.suggestions.map((sg) => (
            <div key={sg.label} className="mini" style={{ padding: '3px 0' }}>
              <b>{sg.label}</b> ${sg.price} — {sg.rationale}
            </div>
          ))}
        </div>
      )}
      {data.caveats?.length > 0 && (
        <div className="mini muted" style={{ marginTop: 6 }}>⚠ {data.caveats.join('; ')}</div>
      )}
    </div>
  )
}

/** Demand over time for one listing.
 *
 * Inline SVG rather than a charting library: the series is at most 30 points,
 * the only interaction is "what is this bar", and a library would add ~100KB
 * plus a second styling system to keep in sync with the cards around it.
 *
 * Two series are drawn on one axis because they answer different questions and
 * the gap between them is the story: `events` is how many searches happened,
 * `people` is how many different buyers did them. Twenty searches from three
 * people is three determined buyers; twenty from twenty is a market. Plotting
 * only one of them lets the seller read the wrong one.
 */
function DemandChart({ series, bucket }) {
  if (!series?.length) return null
  const W = 420
  const H = 130
  const PAD_L = 28
  const PAD_B = 18
  const PAD_T = 8
  const innerW = W - PAD_L - 6
  const innerH = H - PAD_B - PAD_T
  const max = Math.max(1, ...series.map((b) => b.events))
  const step = innerW / series.length
  const barW = Math.max(2, Math.min(18, step * 0.62))
  const y = (v) => PAD_T + innerH - (v / max) * innerH

  // Label every bucket when they fit, otherwise thin them out — overlapping
  // tick text is worse than fewer ticks.
  const labelEvery = Math.ceil(series.length / 7)

  const linePath = series
    .map((b, i) => `${i === 0 ? 'M' : 'L'} ${PAD_L + step * i + step / 2} ${y(b.people)}`)
    .join(' ')

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width="100%"
      role="img"
      aria-label={`Searches per ${bucket} with distinct buyers overlaid`}
      style={{ display: 'block', marginTop: 8, overflow: 'visible' }}
    >
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line
            x1={PAD_L} x2={W - 6} y1={y(max * f)} y2={y(max * f)}
            stroke="var(--line)" strokeWidth="1"
          />
          <text x={0} y={y(max * f) + 3} fontSize="9" fill="var(--ink-3)">
            {Math.round(max * f)}
          </text>
        </g>
      ))}

      {series.map((b, i) => {
        const x = PAD_L + step * i + (step - barW) / 2
        return (
          <g key={b.period}>
            <rect
              x={x} y={y(b.events)} width={barW} height={Math.max(0, PAD_T + innerH - y(b.events))}
              rx="2" fill="var(--brand-soft)" stroke="var(--brand)" strokeWidth="0.75"
            >
              {/* Native tooltip: no state, no portal, works on a touch-free hover
                  and is read out by screen readers. */}
              <title>{`${b.period}：${b.people} 人搜索了 ${b.events} 次${b.views ? `，这件商品被浏览 ${b.views} 次` : ''}`}</title>
            </rect>
            {i % labelEvery === 0 && (
              <text
                x={PAD_L + step * i + step / 2} y={H - 5}
                fontSize="8.5" textAnchor="middle" fill="var(--ink-3)"
              >
                {b.period.replace(/^\d{4}-/, '')}
              </text>
            )}
          </g>
        )
      })}

      <path d={linePath} fill="none" stroke="var(--brand-deep)" strokeWidth="1.5" />
      {series.map((b, i) => (
        <circle
          key={b.period} cx={PAD_L + step * i + step / 2} cy={y(b.people)} r="2"
          fill="var(--brand-deep)"
        />
      ))}
    </svg>
  )
}

const TREND_LABEL = {
  rising: ['上升', 'var(--ok)'],
  falling: ['下降', 'var(--warn-ink)'],
  flat: ['平稳', 'var(--ink-3)'],
  insufficient_data: ['数据不足', 'var(--ink-3)'],
}

/** Demand analysis for one listing. Three numbers are kept visually apart
 * because they mean different things and collapsing them is how a seller ends
 * up acting on the wrong one: the category is the market, `people_matching_item`
 * is the part actually about this listing, and listing traffic is how this
 * listing itself performed inside that market. */
function DemandCard({ data }) {
  if (!data) return null
  const [trendText, trendColor] = TREND_LABEL[data.trend] ?? [data.trend, 'var(--ink-3)']
  const t = data.listing_traffic

  return (
    <div className="card">
      <div className="tiny strong" style={{ marginBottom: 2 }}>
        需求 · {data.item_name}
        {data.category ? ` · ${data.category}` : ''}
      </div>
      <div className="mini muted">{data.window_range} · 按{data.bucket === 'week' ? '周' : '天'}统计</div>

      {data.series?.length > 0 ? (
        <>
          <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            <span className="mini strong" style={{ color: trendColor }}>{trendText}</span>
            {data.trend_detail && <span className="mini muted">{data.trend_detail}</span>}
          </div>

          <DemandChart series={data.series} bucket={data.bucket} />

          <div className="mini muted" style={{ marginTop: 2 }}>
            <span style={{ color: 'var(--brand)' }}>▮</span> 搜索次数 ·{' '}
            <span style={{ color: 'var(--brand-deep)' }}>—</span> 去重买家数
          </div>

          <div className="row" style={{ gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
            {[
              [`${data.people_matching_item}`, '人在找这件'],
              [`${data.total_people}`, `人活跃于${data.category ?? '该类别'}`],
              [`${data.total_events}`, '次搜索'],
              [`${data.active_wants}`, '条有效求购'],
            ].map(([n, label]) => (
              <div key={label} className="mini"
                   style={{ background: 'var(--brand-soft)', borderRadius: 8, padding: '4px 8px' }}>
                <b>{n}</b> {label}
              </div>
            ))}
          </div>

          <div className="mini muted" style={{ marginTop: 6 }}>
            这件商品：曝光 {t.impressions} 次，被打开 {t.clicks} 次（{t.click_through}）
          </div>

          {data.top_phrases?.length > 0 && (
            <div className="mini" style={{ marginTop: 6 }}>
              <span className="muted">买家这样搜：</span>
              {data.top_phrases.map((p, i) => (
                <span key={p.phrase}>
                  {i > 0 && <span className="muted">, </span>}
                  {/* Matching phrasings are the leads; the rest are context for
                      the same category and are dimmed so the two do not read as
                      one list of people who want this item. */}
                  <span style={{
                    color: p.matches_this_item ? 'var(--ink)' : 'var(--ink-3)',
                    fontWeight: p.matches_this_item ? 600 : 400,
                  }}>
                    {p.phrase} ({p.people})
                  </span>
                </span>
              ))}
            </div>
          )}
        </>
      ) : (
        <EmptyNote empty={data.empty}
                   fallback="这个时间窗口内没有搜索——这就是测量结果，不是数据缺失。" />
      )}

      {data.caveats?.length > 0 && (
        <div className="mini muted" style={{ marginTop: 6 }}>⚠ {data.caveats.join('; ')}</div>
      )}
    </div>
  )
}

/** Rules a submission tripped. Blocks are the reason it cannot be listed;
 * warnings are advice the seller can take or leave. Both name the rule, because
 * "not allowed" that cannot be traced to a rule is not actionable. */
function RuleFindings({ findings }) {
  if (!findings?.length) return null
  return (
    <div className="card">
      <div className="tiny strong" style={{ marginBottom: 6 }}>发布规则</div>
      {findings.map((f) => (
        <div key={f.code} className="mini" style={{ padding: '4px 0' }}>
          <b style={{ color: f.severity === 'block' ? 'var(--err, #c33)' : 'inherit' }}>
            {f.severity === 'block' ? '⛔' : '⚠'} {f.title}
          </b>
          <span className="muted"> ({f.code})</span> — {f.reason}
        </div>
      ))}
    </div>
  )
}

/** The draft the seller reviews, edits and publishes.
 *
 * Everything text-shaped is editable and the two whitelist fields are dropdowns:
 * the model proposes, the seller decides. Publishing goes through its own
 * endpoint, which re-checks exactly this payload — so an edit made here is
 * checked, not grandfathered in on the earlier review.
 */
function PublishDraftCard({ draft, config, onPublished }) {
  const [form, setForm] = useState(() => ({
    name: draft.name || '',
    summary: draft.summary || '',
    description: draft.description || '',
    category: draft.category || '',
    subcategory: draft.subcategory || '',
    condition: draft.condition || '',
    brand: draft.brand || '',
    color: draft.color || '',
    material: draft.material || '',
    size: draft.size || '',
    price: '',
    location: '',
  }))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(null)
  const [findings, setFindings] = useState([])

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))
  // 换了大类，原来的子类目就不属于它了——清掉让卖家重选，而不是带着一个对不上的值提交。
  const setCategory = (e) => {
    const category = e.target.value
    setForm((f) => ({
      ...f,
      category,
      subcategory: (config?.subcategories?.[category] ?? []).includes(f.subcategory) ? f.subcategory : '',
    }))
  }
  const subOptions = config?.subcategories?.[form.category] ?? []

  if (done) {
    return (
      <div className="card">
        <div className="tiny strong">✓ 已上架</div>
        <div className="mini muted" style={{ marginTop: 4 }}>{done}</div>
        <RuleFindings findings={findings} />
      </div>
    )
  }

  const publish = async () => {
    if (!form.name.trim() || !form.description.trim()) return setError('标题和描述都要填')
    if (!form.category) return setError('请选择类别')
    if (!form.condition) return setError('请选择成色')
    if (!form.price || Number(form.price) <= 0) return setError('请填写价格')
    if (!form.location) return setError('请选择自提地点')
    setBusy(true)
    setError('')
    try {
      const res = await api.publishApprove({
        thread_id: draft.thread_id,
        name: form.name.trim(),
        summary: form.summary.trim(),
        description: form.description.trim(),
        category: form.category,
        subcategory: form.subcategory || null,
        // 不给卖家看也不让他改；后端会核对它还在不在最终标题里。
        head_noun: draft.head_noun || null,
        condition: form.condition,
        brand: form.brand || null,
        color: form.color || null,
        material: form.material || null,
        size: form.size || null,
        price: Number(form.price),
        pickup_location_name: form.location,
        photo_path: draft.photo_path || '',
      })
      setFindings(res.findings || [])
      if (res.ok) {
        setDone(res.message)
        onPublished?.()
      } else {
        setError(res.message)
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <div className="tiny strong" style={{ marginBottom: 6 }}>
        商品草稿 {draft.ready ? '· 可以发布了' : '· 还差一些信息'}
      </div>
      {draft.photo_path && (
        <img src={`/static/${draft.photo_path}`} alt=""
             style={{ maxWidth: 160, borderRadius: 8, marginBottom: 8 }} />
      )}
      <div className="field">
        <label className="mini">标题</label>
        <input className="input" value={form.name} onChange={set('name')} maxLength={40} />
      </div>
      <div className="field">
        <label className="mini">一句话摘要</label>
        <input className="input" value={form.summary} onChange={set('summary')} maxLength={120} />
      </div>
      <div className="field">
        <label className="mini">描述</label>
        <textarea className="input" value={form.description} onChange={set('description')} maxLength={600} />
      </div>
      <div className="row">
        <div className="field" style={{ flex: 1 }}>
          <label className="mini">类别 · 必填</label>
          <select className="input" value={form.category} onChange={setCategory}>
            <option value="">请选择</option>
            {(config?.categories ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label className="mini">子类目</label>
          <select className="input" value={form.subcategory} onChange={set('subcategory')}
                  disabled={!subOptions.length}>
            <option value="">{form.category ? '不确定就留空' : '先选类别'}</option>
            {subOptions.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      </div>
      <div className="row">
        <div className="field" style={{ flex: 1 }}>
          <label className="mini">成色 · 必填</label>
          <select className="input" value={form.condition} onChange={set('condition')}>
            <option value="">请选择</option>
            {CONDITIONS.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      </div>
      <div className="row">
        <div className="field" style={{ flex: 1 }}>
          <label className="mini">品牌</label>
          <input className="input" value={form.brand} onChange={set('brand')} />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label className="mini">颜色</label>
          <input className="input" value={form.color} onChange={set('color')} />
        </div>
      </div>
      <div className="row">
        <div className="field" style={{ flex: 1 }}>
          <label className="mini">价格（美元）· 你自己定</label>
          <input className="input" type="number" min="1" value={form.price} onChange={set('price')} />
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label className="mini">自提地点</label>
          <select className="input" value={form.location} onChange={set('location')}>
            <option value="">请选择</option>
            {(config?.locations ?? []).map((l) => <option key={l.name} value={l.name}>{l.name}</option>)}
          </select>
        </div>
      </div>
      {draft.missing_fields?.length > 0 && (
        <div className="mini muted">还没确认：{draft.missing_fields.map((f) => FIELD_LABEL[f] ?? f).join('、')}</div>
      )}
      {error && <div className="mini" style={{ color: 'var(--err, #c33)', marginTop: 6 }}>{error}</div>}
      <RuleFindings findings={findings} />
      <button className="btn block" style={{ marginTop: 8 }} disabled={busy} onClick={publish}>
        {busy ? '发布中…' : '确认并发布'}
      </button>
      <div className="mini muted" style={{ marginTop: 6 }}>
        你不点这个按钮，什么都不会保存。发布时会再按发布规则检查一遍。
      </div>
    </div>
  )
}

/** The conversation rail.
 *
 * Renaming is inline rather than a modal: the title is one short string and the
 * list is where you are already looking. Deleting asks first — a conversation
 * holds a transcript the seller cannot get back, and the row is one pixel from
 * the row they meant to open.
 */
function ThreadRail({ threads, activeId, busy, onOpen, onNew, onRename, onDelete }) {
  const [editing, setEditing] = useState(null)   // thread_id being renamed
  const [draftTitle, setDraftTitle] = useState('')
  const [confirming, setConfirming] = useState(null)

  const commit = async (id) => {
    setEditing(null)
    await onRename(id, draftTitle)
  }

  return (
    <aside className="thread-rail" aria-label="会话列表">
      <button className="btn ghost block thread-new" disabled={busy} onClick={onNew}>
        + 新建会话
      </button>
      <div className="thread-list">
        {threads.length === 0 && (
          <div className="mini muted" style={{ padding: '8px 10px' }}>还没有会话。</div>
        )}
        {threads.map((t) => {
          const active = t.thread_id === activeId
          if (editing === t.thread_id) {
            return (
              <div key={t.thread_id} className="thread-row is-editing">
                <input className="input thread-input" autoFocus value={draftTitle}
                       maxLength={80}
                       onChange={(e) => setDraftTitle(e.target.value)}
                       onBlur={() => commit(t.thread_id)}
                       onKeyDown={(e) => {
                         if (e.key === 'Enter') commit(t.thread_id)
                         if (e.key === 'Escape') setEditing(null)
                       }} />
              </div>
            )
          }
          return (
            <div key={t.thread_id} className={`thread-row${active ? ' active' : ''}`}>
              <button className="thread-open" disabled={busy} onClick={() => onOpen(t.thread_id)}
                      title={t.title}>
                <span className="thread-title">{t.title}</span>
                <span className="thread-when">{t.updated_at}</span>
              </button>
              <span className="thread-actions">
                <button title="重命名" aria-label="重命名会话"
                        onClick={() => { setEditing(t.thread_id); setDraftTitle(t.title) }}>✎</button>
                <button title="删除" aria-label="删除会话"
                        onClick={() => setConfirming(t.thread_id)}>🗑</button>
              </span>
              {confirming === t.thread_id && (
                <div className="thread-confirm">
                  <span className="mini">删除这个会话？</span>
                  <button className="mini danger" onClick={async () => {
                    setConfirming(null)
                    await onDelete(t.thread_id)
                  }}>删除</button>
                  <button className="mini" onClick={() => setConfirming(null)}>取消</button>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </aside>
  )
}

/**
 * My Sales Agent — ChatGPT-style single column.
 *
 * The transcript scrolls in the middle, the composer is docked at the bottom, and
 * the structured results (buyers / pricing) render inline underneath the reply
 * that produced them.
 *
 * There is no item picker anywhere: which listing a question is about is settled
 * inside the conversation. The backend injects the seller's active listings as
 * facts and re-checks ownership on every item the model names, so the seller can
 * just say "the bike" — or ask about all of them at once — instead of pre-selecting.
 *
 * The transcript rendered here is a display copy. The conversation the model
 * actually sees lives on the server behind `threadId`; this component only
 * carries that id back. "Clear chat" therefore drops the thread too — otherwise
 * the screen would look empty while the model kept remembering.
 */
export default function SellerAdvisor({ config, refreshKey, onListed }) {
  const [activeCount, setActiveCount] = useState(null)   // null = still loading
  const [threadId, setThreadId] = useState(null)   // server-side conversation handle
  const [threads, setThreads] = useState([])      // the rail's list
  // 出售计划监控的总开关。开局读一次，之后**每一轮回复都可能改写它**——卖家
  // 可以用嘴关掉，那时候这个按钮必须跟着灭。
  const [watch, setWatch] = useState(null)
  const [watchBusy, setWatchBusy] = useState(false)
  const [photo, setPhoto] = useState(null)        // {photo_path, photo_url} for the next message
  const [uploading, setUploading] = useState(false)
  const [dragging, setDragging] = useState(false)
  const dragDepth = useRef(0)                    // see onDragEnter for why this is a counter
  const [turns, setTurns] = useState([])   // display copy of the transcript
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const endRef = useRef(null)
  const inputRef = useRef(null)

  const loadThreads = async (selectId) => {
    try {
      const rows = await api.advisorThreads()
      setThreads(rows)
      if (selectId !== undefined) setThreadId(selectId)
      return rows
    } catch (e) {
      // Keep the list on screen. Emptying it on a failed refresh made every
      // conversation look deleted (it was a 503 from the rate limiter, 2026-10-10).
      // The action that triggered the refresh did succeed, so the selection
      // still moves to it.
      if (selectId !== undefined) setThreadId(selectId)
      setError(`会话列表刷新失败：${e.message}`)
      return null
    }
  }

  useEffect(() => { loadThreads() }, [])

  const loadWatch = async () => {
    try {
      setWatch(await api.saleWatch())
    } catch {
      // 读不到就不显示这个按钮。一个状态未知的开关，比没有开关更容易误导人。
      setWatch(null)
    }
  }
  useEffect(() => { loadWatch() }, [])

  const toggleWatch = async (next) => {
    setWatchBusy(true)
    try {
      setWatch(await api.setSaleWatch(next))
    } catch (e) {
      setError(e.message)
    } finally {
      setWatchBusy(false)
    }
  }

  // 卡片上的两个按钮。改完之后重新读一次总开关，因为「其中几条正在盯」这个数
  // 会跟着变——按钮上写着的那个数字必须是真的。
  const togglePlanWatch = async (planId, next) => {
    setWatchBusy(true)
    try {
      const row = await api.setSalePlanWatch(planId, next)
      setTurns((prev) => prev.map((t) => (
        t.salePlans?.some((p) => p.plan_id === planId)
          ? { ...t, salePlans: t.salePlans.map((p) => (p.plan_id === planId ? row : p)) }
          : t
      )))
      loadWatch()
    } catch (e) {
      setError(e.message)
    } finally {
      setWatchBusy(false)
    }
  }

  const cancelPlan = async (planId, name) => {
    // 撤销是不可逆的（要重排），所以这里问一句；暂停不问，因为它一键就能开回来。
    if (!window.confirm(`撤销「${name}」的出售计划？撤掉之后要重新排，暂停的话随时能开回来。`)) return
    setWatchBusy(true)
    try {
      await api.cancelSalePlan(planId)
      setTurns((prev) => prev.map((t) => (
        t.salePlans?.some((p) => p.plan_id === planId)
          ? { ...t, salePlans: t.salePlans.map((p) => (p.plan_id === planId ? { ...p, status: 'cancelled', watching_now: false } : p)) }
          : t
      )))
      loadWatch()
    } catch (e) {
      setError(e.message)
    } finally {
      setWatchBusy(false)
    }
  }

  /** Opening a conversation replaces the transcript on screen with the one the
   * server has. Structured cards are not restored — they come from live tool
   * results, and showing a stored copy would present yesterday's buyers as
   * current. */
  const openThread = async (id) => {
    if (id === threadId || busy) return
    setError('')
    try {
      const data = await api.advisorThread(id)
      setThreadId(data.thread_id)
      setTurns(data.turns.map((t) => ({ role: t.role, content: t.content })))
    } catch (e) {
      setError(e.message)
    }
  }

  const newThread = async () => {
    setError('')
    try {
      const row = await api.newAdvisorThread()
      setTurns([])
      setPhoto(null)
      await loadThreads(row.thread_id)
    } catch (e) {
      setError(e.message)
    }
  }

  const renameThread = async (id, title) => {
    try {
      await api.renameAdvisorThread(id, title)
      await loadThreads()
    } catch (e) {
      setError(e.message)
    }
  }

  const deleteThread = async (id) => {
    try {
      await api.deleteAdvisorThread(id)
      const rows = await loadThreads()
      // Deleting the open conversation leaves the panel showing a transcript
      // that no longer exists — clear it rather than leave a ghost.
      if (id === threadId) {
        setThreadId(null)
        setTurns([])
      }
      return rows
    } catch (e) {
      setError(e.message)
    }
  }

  // Only used for the empty-state hint — the advisor itself no longer needs the
  // frontend to know anything about which listings exist.
  useEffect(() => {
    api.sellerItems()
      .then((rows) => setActiveCount(
        rows.filter((r) => r.status === 'selling' || r.status === 'reserved').length,
      ))
      .catch(() => setActiveCount(0))
  }, [refreshKey])

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns, busy])

  /** The one upload path. The file picker, a drop and a paste all land here so
   * the three entry points cannot drift apart in what they accept or compress. */
  const attachFile = async (file) => {
    if (!file) return
    if (!file.type?.startsWith('image/')) {
      return setError('这不是图片——请附一张商品照片。')
    }
    setUploading(true)
    setError('')
    try {
      const compressed = await compressImage(file)
      setPhoto(await api.uploadPhoto(compressed))
    } catch (err) {
      setError(err.message)
    } finally {
      setUploading(false)
    }
  }

  const pickPhoto = async (e) => {
    await attachFile(e.target.files?.[0])
    e.target.value = ''
  }

  /** Drag state is a counter, not a boolean.
   *
   * dragenter/dragleave fire for every child element the cursor crosses, so a
   * boolean flickers the whole time you move across the transcript. Counting
   * enters minus leaves is the standard fix and the only one that stays steady
   * over a busy subtree.
   */
  const onDragEnter = (e) => {
    if (!e.dataTransfer?.types?.includes('Files')) return
    e.preventDefault()
    dragDepth.current += 1
    setDragging(true)
  }

  const onDragOver = (e) => {
    if (!e.dataTransfer?.types?.includes('Files')) return
    e.preventDefault()          // without this the browser navigates to the file
    e.dataTransfer.dropEffect = 'copy'
  }

  const onDragLeave = (e) => {
    if (!e.dataTransfer?.types?.includes('Files')) return
    e.preventDefault()
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragging(false)
  }

  const onDrop = async (e) => {
    if (!e.dataTransfer?.types?.includes('Files')) return
    e.preventDefault()
    dragDepth.current = 0
    setDragging(false)
    if (busy || uploading) return
    const files = [...(e.dataTransfer.files || [])]
    // One photo per listing, so take the first and say so rather than
    // silently dropping the rest.
    if (files.length > 1) setError('一次只能附一张照片——已使用第一张。')
    await attachFile(files[0])
  }

  /** Pasting a screenshot is the same gesture as dropping one, and costs two lines. */
  const onPaste = async (e) => {
    const file = [...(e.clipboardData?.files || [])][0]
    if (file) {
      e.preventDefault()
      await attachFile(file)
    }
  }

  const send = async (text) => {
    const message = (text ?? input).trim()
    if (!message || busy) return
    setInput('')
    setError('')
    const attached = photo
    setPhoto(null)
    const nextTurns = [...turns, { role: 'user', content: message, photo: attached?.photo_url }]
    setTurns(nextTurns)
    setBusy(true)
    try {
      const res = await api.advisorChat(message, threadId, attached?.photo_path)
      if (res.thread_id) setThreadId(res.thread_id)
      // The title is auto-filled from the first message, and a brand-new
      // conversation only appears in the list once it has one.
      loadThreads()
      setTurns((prev) => [...prev, {
        role: 'assistant',
        content: res.reply || res.message || '（没有回复）',
        degraded: res.degraded,
        tools: res.used_tools,
        route: res.route,
        buyers: res.potential_buyers || [],
        pricing: res.pricing_analysis || [],
        demand: res.demand_analysis || [],
        comparison: res.comparison || null,
        // 计划卡片直接来自工具输出。`sale_plan`（单数）是没有 API key 时那条
        // 确定性兜底路径留下的，两个都收。
        salePlans: res.sale_plans?.length ? res.sale_plans : (res.sale_plan ? [res.sale_plan] : []),
        planDemand: res.plan_demand || [],
        // The draft carries the thread id so publishing targets this conversation
        draft: res.publish_draft ? { ...res.publish_draft, thread_id: res.thread_id } : null,
        salePlan: res.sale_plan || null,
        findings: res.publish_findings || [],
        note: res.ok ? res.message : '',
        // Kept on the turn, not in a running total: this is what *this*
        // question cost, and a thread reopened from the transcript has no
        // token history to show, so a session total would be quietly partial.
        usage: {
          input: res.input_tokens || 0,
          output: res.output_tokens || 0,
          calls: res.llm_calls || 0,
          detail: res.token_detail || {},
        },
      }])
      // 这一轮碰过计划的话，按钮跟着回复走。`watch_enabled` 为 null 表示这一轮
      // 和计划无关——那时候不要动它，否则按钮会在无关的对话里乱闪。
      if (res.watch_enabled !== null && res.watch_enabled !== undefined) loadWatch()
      if (res.no_active_items) setActiveCount(0)
      if (!res.ok && res.message) setError(res.message)
    } catch (e) {
      setError(e.message)
      setTurns((prev) => [...prev, { role: 'assistant', content: `出错了：${e.message}` }])
    } finally {
      setBusy(false)
      inputRef.current?.focus()
    }
  }

  const empty = turns.length === 0

  return (
    <div className="advisor-workspace">
      <ThreadRail threads={threads} activeId={threadId} busy={busy}
                  onOpen={openThread} onNew={newThread}
                  onRename={renameThread} onDelete={deleteThread} />
    <div className={`advisor-shell${dragging ? ' is-dropping' : ''}`}
         onDragEnter={onDragEnter} onDragOver={onDragOver}
         onDragLeave={onDragLeave} onDrop={onDrop}>
      {/* The whole panel is the drop target, not just the composer: by the time
          you are dragging a file you are looking at the conversation, and a
          small target means a file dropped on the wrong pixel opens in the
          browser and loses the page. */}
      {dragging && (
        <div className="advisor-dropzone">
          <div>
            <div style={{ fontSize: 28, marginBottom: 6 }}>🖼️</div>
            松手即可附上这张照片
            <div className="mini muted" style={{ marginTop: 4 }}>每件商品一张图</div>
          </div>
        </div>
      )}
      <WatchSwitch state={watch} busy={watchBusy} onToggle={toggleWatch} />
      {/* ---------- Scrolling transcript ---------- */}
      <div className="advisor-stream">
        <div className={`advisor-thread${empty ? ' is-empty' : ''}`}>
          {empty ? (
            <div className="advisor-hero">
              <h2>我的销售助手</h2>
              <p className="mini muted">
                {activeCount === 0
                  ? '先在「手动上架」挂一件商品——顾问是基于你的在售商品做分析的。'
                  : <>正在读取你 {activeCount ?? ''} 件在售商品的真实数据。直接说是哪一件，或者一次问全部。</>}
              </p>
              <div className="suggests hero-suggests">
                {SUGGESTS.map((s) => (
                  <button key={s} type="button" disabled={busy} onClick={() => send(s)}>{s}</button>
                ))}
              </div>
            </div>
          ) : (
            turns.map((t, i) => (
              <div key={i} className={`msg ${t.role === 'user' ? 'user' : 'ai'}`}>
                <div className="msg-body">{t.content}</div>
                {t.degraded && <div className="notice warn">顾问处于降级状态：没有配置 LLM API key。</div>}
                {t.note && <div className="mini muted">{t.note}</div>}
                {t.photo && (
                  <img src={t.photo} alt="" style={{ maxWidth: 140, borderRadius: 8, marginTop: 6 }} />
                )}
                {/* Every kind of attachment has to be named here, including the
                    ones whose component handles its own empty case: this gate
                    decides whether the container renders at all. `demand` was
                    missing, so a turn that only analyzed demand — "最近的需求趋势
                    给我看看" — computed the chart, sent it, and drew nothing. */}
                {(t.buyers?.length > 0 || t.pricing?.length > 0 || t.demand?.length > 0
                  || t.findings?.length > 0 || t.comparison || t.draft
                  || t.salePlans?.length > 0 || t.planDemand?.length > 0) && (
                  <div className="msg-attachments">
                    {t.comparison && <ComparisonCard data={t.comparison} />}
                    {t.buyers?.map((b) => <BuyerCards key={`b${b.item_id}`} data={b} />)}
                    {t.pricing?.map((p) => <PricingCard key={`p${p.item_id}`} data={p} />)}
                    {t.demand?.map((d) => <DemandCard key={`d${d.item_id}`} data={d} />)}
                    {t.salePlans?.map((p) => (
                      <SalePlanCard key={`sp${p.plan_id}`} plan={p} busy={watchBusy}
                                    onToggle={togglePlanWatch} onCancel={cancelPlan} />
                    ))}
                    {t.planDemand?.map((d) => <PlanDemandCard key={`pd${d.plan_id}`} data={d} />)}
                    <RuleFindings findings={t.findings} />
                    {t.draft && (
                      <PublishDraftCard draft={t.draft} config={config} onPublished={onListed} />
                    )}
                  </div>
                )}
                {t.role === 'assistant' && <SourceBadges tools={t.tools} route={t.route} usage={t.usage} />}
              </div>
            ))
          )}

          {busy && (
            <div className="msg ai">
              <div className="msg-body muted">顾问正在思考…</div>
            </div>
          )}
          {error && <div className="notice err">{error}</div>}
          {!config?.llm_available && (
            <div className="notice warn">
              没有配置 LLM API key，销售顾问暂时不可用；上架和其他功能都不受影响。
            </div>
          )}
          <div ref={endRef} />
        </div>
      </div>

      {/* ---------- Docked composer ---------- */}
      <div className="advisor-dock">
        <form className="chat-box" onSubmit={(e) => { e.preventDefault(); send() }}>
          <input ref={inputRef}
                 onPaste={onPaste}
                 className="chat-input"
                 value={input}
                 onChange={(e) => setInput(e.target.value)}
                 maxLength={500}
                 placeholder="问问买家或者定价…也可以拖一张照片进来上架"
                 disabled={busy} />
          <div className="chat-tools">
            <label className="chat-plain" style={{ cursor: 'pointer' }}
                   title="附一张照片——上架商品时会用到">
              {uploading ? '上传中…' : photo ? '📎 1 张照片' : '📎 照片'}
              <input type="file" accept="image/*" hidden onChange={pickPhoto} disabled={busy || uploading} />
            </label>
            {photo && (
              <button type="button" className="chat-plain" onClick={() => setPhoto(null)}>移除</button>
            )}
            <span className="chat-tools-gap" />
            <button type="button" className="chat-plain" disabled={busy || empty}
                    onClick={() => { setTurns([]); setThreadId(null); setError('') }}>
              清空对话
            </button>
            <button className="chat-send" aria-label="发送" disabled={busy || !input.trim()}>↑</button>
          </div>
        </form>
        <p className="advisor-disclaimer">{DISCLAIMER}</p>
      </div>
    </div>
    </div>
  )
}
