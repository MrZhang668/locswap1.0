import { useState } from 'react'
import { api } from '../api'
import { Empty, StatusPill, useAsync } from '../components/common'

/**
 * Messages page: the system Inbox.
 *
 * Seller-side item management used to live here as a "My Listings" tab; it has
 * moved into My Selling · My Showcase, where the listing wall already was — one
 * place to look at your own items instead of two. What is left is one list, so
 * the tab bar went with it.
 *
 * Same quiet palette as Cart and Profile: ink / grey type on the page background,
 * no colour chips, no card borders.
 */
export default function Messages({ onChanged, notifications, reloadNotifications }) {
  const unread = notifications.filter((n) => !n.read).length
  return (
    <div className="page-scroll">
      <div className="container narrow quiet-page">
        <div className="page-head stack">
          <h1>消息</h1>
          <span className="sub">{unread > 0 ? `${unread} 条未读` : '全部已读'}</span>
        </div>
        <Notifications rows={notifications} reload={reloadNotifications} onChanged={onChanged} />
      </div>
    </div>
  )
}

/** The listing a seller pushed, fetched live rather than replayed from the
 *  notification payload: between the push and the buyer opening it the item may
 *  have been reserved or sold, and a stale "Reserve" button is worse than none.
 */
function PushedItem({ itemId, onChanged }) {
  const { data: card, loading, error, reload } = useAsync(() => api.item(itemId), [itemId])
  const [busy, setBusy] = useState(false)
  const [tip, setTip] = useState('')

  if (loading) return <div className="mini muted" style={{ marginTop: 8 }}>正在加载商品…</div>
  if (error || !card) {
    return <div className="mini muted" style={{ marginTop: 8 }}>这件商品已经下架了。</div>
  }

  const reserve = async (e) => {
    e.stopPropagation()
    setBusy(true)
    setTip('')
    try {
      await api.reserve(card.item_id)
      await reload()
      onChanged?.()
      setTip('已预订。卖家现在能看到你的联系方式，他的联系方式也已经在你的购物车里。')
    } catch (err) {
      setTip(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="pushed-item" onClick={(e) => e.stopPropagation()}>
      <div className="row" style={{ gap: 12, alignItems: 'center' }}>
        {card.photo_url && (
          <img src={card.photo_url} alt="" width={56} height={56}
               style={{ objectFit: 'cover', borderRadius: 8, flex: 'none' }} />
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="tiny strong">{card.name}</div>
          <div className="mini muted">{card.summary}</div>
          <div className="mini muted" style={{ marginTop: 3 }}>
            <StatusPill status={card.status} bare />
            {card.distance_km != null && ` · 距离 ${card.distance_km.toFixed(1)} 公里`}
          </div>
        </div>
        <div className="listing-price sm">${Number(card.price).toFixed(0)}</div>
      </div>
      <div className="row between" style={{ marginTop: 10, alignItems: 'center' }}>
        <span className="mini muted">完全由你决定——你不点，就不会预订。</span>
        {card.reserved_by_me ? (
          <span className="mini strong">✓ 已预订（第 {card.my_rank} 位）</span>
        ) : (
          <button className="btn ink sm" disabled={busy} onClick={reserve}>
            {busy ? '预订中…' : '预订'}
          </button>
        )}
      </div>
      {tip && <div className="mini muted" style={{ marginTop: 6 }}>{tip}</div>}
    </div>
  )
}

/* Each notification type gets its own colour, so a "Deal confirmed" and a
 * "Reservation expired" are told apart before the title is even read. The colour
 * drives the left rail, the title and the unread dot at once (see --tone). */
const TYPE_TONE = {
  deal_confirmed: 'ok',       // green  — it worked out
  want_matched: 'brand',      // blue   — something to act on
  item_expired: 'warn',       // orange — the window ran out
  deal_lost: 'danger',        // red    — it went to someone else
  item_delisted: 'grey',      // grey   — the seller withdrew it
}

function Notifications({ rows, reload, onChanged }) {
  const [busy, setBusy] = useState(false)

  const readAll = async () => {
    setBusy(true)
    try { await api.markAllRead(); await reload() } finally { setBusy(false) }
  }
  // Opening = mark read + record one item click (E6). Clicking an already-read one still counts:
  // "I came back for another look" and "I glanced at it" are signals of different strength.
  const openOne = async (id) => { await api.clickNotification(id); await reload() }

  return (
    <>
      {rows.length === 0 ? (
        <Empty>还没有系统消息。</Empty>
      ) : (
        <>
          <div className="row between inbox-bar">
            <span className="tiny muted">共 {rows.length} 条</span>
            <button className="quiet-btn sm" onClick={readAll} disabled={busy}>全部标为已读</button>
          </div>
          <ul className="listing-list">
            {rows.map((n) => (
              <li className={`note-row tone-${TYPE_TONE[n.type] ?? 'grey'} ${n.read ? 'read' : ''}`}
                  key={n.id} onClick={() => openOne(n.id)}
                  role="button" tabIndex={0}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openOne(n.id) } }}>
                <div className="note-head">
                  <span className="t">
                    {!n.read && <i className="dot" aria-hidden="true" />}
                    {n.title}
                  </span>
                  <span className="d">{new Date(n.created_at + 'Z').toLocaleString('zh-CN')}</span>
                </div>
                <div className="note-body">{n.body}</div>
                {n.type === 'want_matched' && n.payload?.item_id && (
                  <PushedItem itemId={n.payload.item_id} onChanged={onChanged} />
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  )
}
