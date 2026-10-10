import { useState } from 'react'
import { api } from '../api'
import { Countdown, Empty, Modal, Toast, useAsync } from '../components/common'

/**
 * The Cart page = two "things I'm waiting on" lists:
 *   My Reservations — items I've grabbed that are waiting for seller confirmation;
 *   Waiting For — things that haven't shown up yet (standing_want), pushed when they arrive.
 * They share a page because, for a buyer, they're two stages of the same thing.
 *
 * Layout note: this page is deliberately quiet — no cards, no borders, no colour
 * chips. Rows sit on the page background and are separated by whitespace alone, so
 * the eye lands on the item name and the price, the only two things a buyer scans
 * for here.
 */
export default function Cart(props) {
  const [tab, setTab] = useState('reserved')
  return (
    <div className="page-scroll">
      <div className="container narrow cart-page">
        <div className="page-head stack">
          <h1>购物车</h1>
          <span className="sub">你已经预订的商品，以及还在等的东西</span>
        </div>
        <div className="tabs plain" role="tablist">
          <button role="tab" aria-selected={tab === 'reserved'}
                  className={tab === 'reserved' ? 'active' : ''} onClick={() => setTab('reserved')}>
            我的预订{props.rows.length > 0 && `（${props.rows.length}）`}
          </button>
          <button role="tab" aria-selected={tab === 'wants'}
                  className={tab === 'wants' ? 'active' : ''} onClick={() => setTab('wants')}>
            等着的东西
          </button>
        </div>
        {tab === 'reserved' ? <Reserved {...props} /> : <Wants />}
      </div>
    </div>
  )
}

/** Thin line icons — the emoji versions sat too heavy on a page this quiet. */
function PinIcon() {
  return (
    <svg className="ic" viewBox="0 0 16 16" aria-hidden="true">
      <path d="M8 14.4c2.6-3.2 4.1-5.4 4.1-7.4a4.1 4.1 0 1 0-8.2 0c0 2 1.5 4.2 4.1 7.4Z" />
      <circle cx="8" cy="6.7" r="1.9" />
    </svg>
  )
}

function ClockIcon() {
  return (
    <svg className="ic" viewBox="0 0 16 16" aria-hidden="true">
      <circle cx="8" cy="8" r="5.9" />
      <path d="M8 4.7V8l2.4 1.6" />
    </svg>
  )
}

/** "Waiting For": the buyer-side view of standing_want. */
function Wants() {
  const { data, loading, error, reload } = useAsync(() => api.wants(), [])
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [tip, setTip] = useState('')

  const rows = data ?? []
  const active = rows.filter((w) => w.status === 'active')

  const add = async (e) => {
    e.preventDefault()
    const v = text.trim()
    if (!v || busy) return
    setBusy(true)
    try {
      await api.createWant(v)
      setText('')
      setTip('记下了，等它出现我们会通知你。')
      await reload()
    } catch (err) {
      setTip(err.message)
    } finally {
      setBusy(false)
    }
  }

  const close = async (id) => {
    setBusy(true)
    try { await api.closeWant(id); await reload() } catch (err) { setTip(err.message) } finally { setBusy(false) }
  }

  if (loading) return <div className="empty">加载中…</div>

  return (
    <>
      <Toast message={error} tone="err" />
      <Toast message={tip} tone="info" />

      <form className="row want-add" onSubmit={add}>
        <input className="input" style={{ flex: 1 }} value={text} onChange={(e) => setText(e.target.value)}
               placeholder="想要但还没找到的东西…" maxLength={300} />
        <button className="btn" disabled={busy || !text.trim()}>添加</button>
      </form>

      {rows.length === 0 ? (
        <Empty>
          你还没有在等什么东西。<br />
          搜索没结果时点「有货通知我」，它就会出现在这里。
        </Empty>
      ) : (
        <>
          <div className="cart-note top">
            {active.length} 条生效中 · 有匹配的商品上架时会推送给你
          </div>
          <ul className="cart-list">
            {rows.map((w) => (
              <li className={`cart-row want ${w.status === 'active' ? '' : 'done'}`} key={w.want_id}>
                <div className="cart-main">
                  <div className="cart-title">
                    <span className="n">{w.text || w.raw_query}</span>
                    <span className="m">{WANT_LABEL[w.status] ?? w.status}</span>
                    {w.filters.category && <span className="m">{w.filters.category}</span>}
                    {w.filters.max_price != null && <span className="m">${w.filters.max_price} 以内</span>}
                    {w.filters.max_distance_km != null && <span className="m">{w.filters.max_distance_km} 公里内</span>}
                    {w.source === 'explicit' && <span className="m">已订阅</span>}
                  </div>
                  <div className="cart-seller">
                    {w.status === 'active' ? `${w.days_left} 天后过期` : '已结束'}
                    {w.notify_count > 0 && ` · 已通知 ${w.notify_count} 次`}
                  </div>
                </div>
                <div className="cart-side">
                  {w.status === 'active' && (
                    <button className="cart-link" disabled={busy}
                            onClick={() => close(w.want_id)}>不等了</button>
                  )}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  )
}

const WANT_LABEL = { active: '等待中', fulfilled: '已满足', expired: '已过期', dismissed: '已关闭' }

/** Items I've reserved. This is the ONLY place that shows the seller's contact info. */
function Reserved({ rows, loading, error, reload, onChanged }) {
  const [confirming, setConfirming] = useState(null)
  const [busy, setBusy] = useState(false)
  const [tip, setTip] = useState('')

  const doCancel = async () => {
    setBusy(true)
    try {
      await api.cancel(confirming.item_id)
      setConfirming(null)
      setTip('预订已取消')
      await reload()
      onChanged?.()
    } catch (e) {
      setTip(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (loading) return <div className="empty">加载中…</div>

  return (
    <>
      <Toast message={error} tone="err" />
      <Toast message={tip} tone="info" />

      {rows.length === 0 ? (
        <Empty>还没有预订任何商品。<br />去「搜索」说一句你想要什么就行。</Empty>
      ) : (
        <ul className="cart-list">
          {rows.map((row) => (
            <li className="cart-row" key={row.item_id}>
              {row.photo_url
                ? <img className="thumb" src={row.photo_url} alt="" />
                : <div className="thumb" />}

              <div className="cart-main">
                <div className="cart-title">
                  <span className="n">{row.name}</span>
                  {row.category && <span className="m">{row.category}</span>}
                  {row.distance_km !== null && <span className="m">{row.distance_km} 公里</span>}
                  <span className="m">你排在第 {row.my_rank} 位</span>
                </div>
                <div className="cart-meta">
                  <span><PinIcon />{row.pickup_location_name}</span>
                  <span><ClockIcon />{row.pickup_time_range}</span>
                </div>
                <div className="cart-seller">{row.seller_contact}</div>
              </div>

              <div className="cart-side">
                <div className="cart-price">${Number(row.price).toFixed(0)}</div>
                {row.deal_confirmed
                  ? <span className="bare-status ok">交易已确认</span>
                  : <Countdown seconds={row.seconds_left} prefix="卖家还剩" bare />}
                {!row.deal_confirmed && (
                  <button className="cart-link danger" onClick={() => setConfirming(row)}>取消预订</button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="cart-note">
        说明：如果卖家把商品确认给了别人，或者确认时限走完，这里的条目会自动消失，
        并在「消息 · 收件箱」留下一条记录。
      </div>

      {confirming && (
        <Modal
          title="取消这笔预订？"
          onClose={() => setConfirming(null)}
          actions={
            <>
              <button className="btn plain sm" onClick={() => setConfirming(null)}>再想想</button>
              <button className="btn solid-danger sm" onClick={doCancel} disabled={busy}>确认取消</button>
            </>
          }
        >
          取消之后，「{confirming.name}」的预订人数会减 1；如果你是最后一个排队的人，
          这件商品会重新变回在售。
        </Modal>
      )}
    </>
  )
}
