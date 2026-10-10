import { useState } from 'react'
import { api, compressImage } from '../api'
import SellerAdvisor from '../components/SellerAdvisor'
import { Countdown, Modal, Toast, useAsync } from '../components/common'

const EMPTY = {
  name: '', description: '', summary: '', price: '',
  category: '', brand: '', condition: '', color: '',
  location: '', timeStart: '', timeEnd: '',
}

const CONDITIONS = ['全新', '几乎全新', '成色很好', '成色良好', '成色一般']

/** My Selling.
 *
 * The agent is the page, not one of three equal tabs: it is where a seller
 * spends their time, and it can now do everything the listing form can. So it
 * opens on arrival with its conversation list beside it, and the two
 * occasional destinations — the showcase, and the manual form for when you
 * already know exactly what you are listing — sit as buttons in the corner.
 * Opening one replaces the workspace and offers a way back; they are errands,
 * not modes you live in.
 */
export default function Sell({ user, config, onChanged }) {
  const [view, setView] = useState('advisor')   // 'advisor' | 'showcase' | 'manual'
  const [listedCount, setListedCount] = useState(0) // after listing, refresh the showcase and the advisor's item list
  const listed = () => { setListedCount((n) => n + 1); onChanged?.() }

  // The advisor is a full-height chat: it takes over the viewport instead of
  // scrolling with the page, so its composer can stay docked at the bottom.
  const fills = view === 'advisor'

  return (
    <div className={fills ? 'page-scroll fills' : 'page-scroll'}>
      <div className="container">
        <div className="page-head selling-head">
          <h1>我要卖</h1>
          <nav className="head-links" aria-label="卖家功能入口">
            {view !== 'advisor' && (
              <button className="head-link" onClick={() => setView('advisor')}>← 我的销售助手</button>
            )}
            <button className={`head-link${view === 'showcase' ? ' active' : ''}`}
                    aria-current={view === 'showcase' ? 'page' : undefined}
                    onClick={() => setView('showcase')}>我的橱窗</button>
            <button className={`head-link${view === 'manual' ? ' active' : ''}`}
                    aria-current={view === 'manual' ? 'page' : undefined}
                    onClick={() => setView('manual')}>手动上架</button>
          </nav>
        </div>

        {view === 'advisor' && (
          <SellerAdvisor config={config} refreshKey={listedCount} onListed={listed} />
        )}
        {view === 'showcase' && (
          <section className="workspace-main">
            <Showcase refreshKey={listedCount} onChanged={listed} />
          </section>
        )}
        {view === 'manual' && (
          <section className="workspace-main">
            <ProductInfo config={config} onListed={listed} />
          </section>
        )}
      </div>
    </div>
  )
}

/** The corner stamp doubles as the status: one glance tells you whether a card is
 *  still on sale. It replaces the status pill rather than sitting next to it. */
const STAMP = {
  selling: ['在售', 'sale'],
  reserved: ['已被预订', 'held'],
  // Paused reads as "held" rather than "gone": the listing still exists and
  // one click puts it back.
  paused: ['已下架', 'held'],
  sold_out: ['已成交', 'gone'],
  expired: ['已过期', 'gone'],
}

function Stamp({ status }) {
  const [text, tone] = STAMP[status] || [status, 'gone']
  return <span className={`stamp ${tone}`}>{text}</span>
}

/** My Showcase: the listing wall doubles as listing management — every card carries
 *  its buyer list and its Delist action, so a seller never leaves this page.
 *  (Merged in from what used to be "Messages · My Listings".)
 *
 *  The buyer list opens in a modal rather than unfolding inside the card: at three
 *  cards per row a card is ~240px wide, which is not enough for names, contact
 *  details and a Confirm deal button.
 */
function Showcase({ refreshKey, onChanged }) {
  const { data, loading, error } = useAsync(() => api.sellerItems(), [refreshKey])
  const [openItem, setOpenItem] = useState(null)   // item whose buyer list is showing
  const [buyers, setBuyers] = useState(null)       // null = still loading
  const [delisting, setDelisting] = useState(null)
  const [busy, setBusy] = useState(false)
  const [tip, setTip] = useState({ text: '', tone: 'info' })

  const items = data ?? []

  const showBuyers = async (item) => {
    setOpenItem(item)
    setBuyers(null)
    try {
      setBuyers(await api.buyers(item.item_id))
    } catch (e) {
      setBuyers([])
      setTip({ text: e.message, tone: 'err' })
    }
  }

  const confirmDeal = async (buyerId) => {
    setBusy(true)
    try {
      await api.confirmDeal(openItem.item_id, buyerId)
      setTip({ text: '交易已确认——买家会收到通知，这件商品也已从搜索中移除。', tone: 'ok' })
      setOpenItem(null)
      onChanged?.()
    } catch (e) {
      setTip({ text: e.message, tone: 'err' })
    } finally {
      setBusy(false)
    }
  }

  // Reversible take-down. The seller says 下架 far more often than they mean
  // 删除, and until now the only thing that word was wired to was permanent
  // deletion.
  const setStatus = async (item, target) => {
    setBusy(true)
    try {
      await api.setItemStatus(item.item_id, target)
      setTip({
        text: target === 'pause'
          ? '已下架。买家搜不到它了，随时可以恢复上架。'
          : '已重新上架。它又回到搜索结果里了。',
        tone: 'ok',
      })
      onChanged?.()
    } catch (e) {
      setTip({ text: e.message, tone: 'err' })
    } finally {
      setBusy(false)
    }
  }

  const doDelist = async () => {
    setBusy(true)
    try {
      await api.delist(delisting.item_id)
      setDelisting(null)
      setOpenItem(null)
      setTip({ text: '商品已永久删除。', tone: 'ok' })
      onChanged?.()
    } catch (e) {
      setTip({ text: e.message, tone: 'err' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Toast message={error} tone="err" />
      <Toast message={tip.text} tone={tip.tone} />

      <div className="card" style={{ display: 'flex', gap: 16, alignItems: 'center' }}>
        <div style={{ flex: 1 }}>
          <div className="strong" style={{ marginBottom: 4 }}>买家能看到什么？</div>
          <div className="tiny muted">
            对外展示的卡片只包含：商品名 / 一句话摘要 / 类别 / 距离 / 价格 / 预订状态。
            你的邮箱和其他个人信息不会进入卡片，也不会进入检索索引；
            只有买家预订成功之后，才能在他的购物车里看到你的联系方式。
          </div>
        </div>
        <span className="pill ok" style={{ flex: 'none' }}>隐私白名单已生效</span>
      </div>

      {loading ? (
        <div className="empty">加载中…</div>
      ) : items.length === 0 ? (
        <div className="empty">还没有发布任何商品。<br />去「手动上架」挂一件试试。</div>
      ) : (
        <div className="showcase-grid">
          {items.map((it) => (
            <div className="showcase-card" key={it.item_id}>
              {it.photo_url ? <img className="ph" src={it.photo_url} alt="" /> : <div className="ph" />}
              <div className="bd">
                <div className="row between" style={{ alignItems: 'baseline' }}>
                  <span className="strong" style={{ fontSize: 14, minWidth: 0,
                        overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {it.name}
                  </span>
                  <span className="price">${Number(it.price).toFixed(0)}</span>
                </div>
                <div className="showcase-meta">
                  {it.reserved_count > 0 && <span className="pill">{it.reserved_count} 人已预订</span>}
                  {it.status === 'reserved' && <Countdown seconds={it.seconds_left} prefix="你还剩" />}
                </div>
                <div className="showcase-sub">
                  上架于 {new Date(it.created_at + 'Z').toLocaleDateString('zh-CN')}
                </div>
              </div>

              {/* Stamp bottom-left, actions bottom-right. The stamp sits on its own
                  row under the title, so it never covers the item name. */}
              <div className="showcase-foot">
                <Stamp status={it.status} />
                <div className="showcase-acts">
                  <button className="quiet-btn sm" onClick={() => showBuyers(it)}>查看预订名单</button>
                  {it.status === 'selling' && (
                    <button className="quiet-btn sm" disabled={busy}
                            onClick={() => setStatus(it, 'pause')}>下架</button>
                  )}
                  {it.status === 'paused' && (
                    <button className="quiet-btn sm" disabled={busy}
                            onClick={() => setStatus(it, 'resume')}>恢复上架</button>
                  )}
                  <button className="quiet-btn sm danger" onClick={() => setDelisting(it)}>删除</button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {openItem && (
        <Modal
          title={`预订买家 — ${openItem.name}`}
          onClose={() => setOpenItem(null)}
          actions={<button className="btn plain sm" onClick={() => setOpenItem(null)}>关闭</button>}
        >
          {buyers === null ? (
            <div className="tiny muted">加载中…</div>
          ) : buyers.length === 0 ? (
            <div className="tiny muted">还没有人预订。</div>
          ) : (
            <ul className="list-reset buyer-list">
              {buyers.map((b) => (
                <li key={b.buyer_id}>
                  <div>
                    <div className="tiny strong">{b.display_name}</div>
                    <div className="mini muted">
                      {new Date(b.reserved_at + 'Z').toLocaleString('zh-CN')}
                      {b.contact && ` · 联系方式 ${b.contact}`}
                    </div>
                  </div>
                  {openItem.status === 'reserved' && (
                    <button className="btn ink sm" disabled={busy}
                            onClick={() => confirmDeal(b.buyer_id)}>确认交易</button>
                  )}
                </li>
              ))}
            </ul>
          )}
          <div className="mini muted" style={{ marginTop: 12 }}>
            买家一旦预订，双方的联系方式同时互相可见——他也能在自己的购物车里看到你的。
            没有预订的买家始终保持匿名。
          </div>
        </Modal>
      )}

      {delisting && (
        <Modal
          title="永久删除这件商品？"
          onClose={() => setDelisting(null)}
          actions={
            <>
              <button className="btn plain sm" onClick={() => setDelisting(null)}>取消</button>
              <button className="btn solid-danger sm" onClick={doDelist} disabled={busy}>永久删除</button>
            </>
          }
        >
          「{delisting.name}」将被<strong>永久删除</strong>且无法恢复；
          已经预订的买家会收到通知。
          <br />
          只是暂时不想卖的话，用「下架」——商品会保留着，随时能恢复上架。
        </Modal>
      )}
    </>
  )
}

/** Product Info: the quick manual listing form, with a live buyer-view preview.
 *
 * No AI on this path any more. It is the express lane — you already know what
 * the item is, you type it, it goes up. The photo-and-a-sentence route lives in
 * My Sales Agent, where the same content is also checked against the listing
 * rules before it goes live; autofilling here would have meant one of the two
 * listing paths quietly skipping that check.
 *
 * Category and condition are required for the same reason they are required
 * server-side: they drive structured search and comparable pricing, and with no
 * autofill left nothing would fill them in later.
 */
function ProductInfo({ config, onListed }) {
  const [form, setForm] = useState(EMPTY)
  const [photo, setPhoto] = useState(null) // {photo_path, photo_url}
  const [uploading, setUploading] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [tip, setTip] = useState({ text: '', tone: 'info' })

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  const pickPhoto = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    setTip({ text: '', tone: 'info' })
    try {
      const compressed = await compressImage(file) // compress client-side first; the backend compresses again
      const res = await api.uploadPhoto(compressed)
      setPhoto(res)
    } catch (err) {
      setTip({ text: err.message, tone: 'err' })
    } finally {
      setUploading(false)
      e.target.value = ''
    }
  }

  const submit = async (e) => {
    e.preventDefault()
    if (!form.name.trim()) return setTip({ text: '请填写商品标题', tone: 'warn' })
    if (!form.category) return setTip({ text: '请选择类别', tone: 'warn' })
    if (!form.condition) return setTip({ text: '请选择成色', tone: 'warn' })
    if (!form.price || Number(form.price) <= 0) return setTip({ text: '请填写价格', tone: 'warn' })
    if (!form.location) return setTip({ text: '请选择自提地点', tone: 'warn' })
    setSubmitting(true)
    setTip({ text: '', tone: 'info' })
    try {
      const res = await api.createItem({
        name: form.name || null,
        description: form.description,
        summary: form.summary || null,
        attributes: {
          category: form.category || null,
          brand: form.brand || null,
          condition: form.condition || null,
          color: form.color || null,
        },
        price: Number(form.price),
        photo_path: photo?.photo_path ?? null,
        pickup_location_name: form.location,
        pickup_time_start: form.timeStart,
        pickup_time_end: form.timeEnd,
      })
      setTip({ text: `已上架：${res.item.name}`, tone: 'ok' })
      setForm(EMPTY)
      setPhoto(null)
      onListed?.()
    } catch (err) {
      setTip({ text: err.message, tone: 'err' })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <form className="col manual-form" onSubmit={submit}>
      <div className="panel-title">01 商品信息</div>

        <div className="card">
          <label className="dropzone" style={{ display: 'block', cursor: 'pointer' }}>
            {photo ? <img src={photo.photo_url} alt="" />
                   : uploading ? '上传中…' : '点击上传商品照片（1 张，上传时会压缩）'}
            <input type="file" accept="image/*" hidden onChange={pickPhoto} />
          </label>
          {photo && (
            <button type="button" className="btn plain sm" style={{ marginTop: 10 }}
                    onClick={() => setPhoto(null)}>移除照片</button>
          )}
        </div>

        <div className="card">
          <div className="field">
            <label>描述</label>
            <textarea className="input" value={form.description} onChange={set('description')}
                      placeholder="比如：用了一年，手感还行，键帽有点打油" />
          </div>
          <div className="mini muted">
            懒得一项项填？去<b>我的销售助手</b>——发一张照片加一句话，
            它会陪你把商品信息写出来，并且按发布规则先检查一遍。
          </div>
          <Toast message={tip.text} tone={tip.tone} />
        </div>

        <div className="card">
          <div className="field">
            <label>商品标题</label>
            <input className="input" value={form.name} onChange={set('name')}
                   placeholder="品牌 + 品类 + 关键特征" required />
          </div>
          <div className="field">
            <label>一句话摘要（不超过 2 句，显示在卡片上）</label>
            <input className="input" value={form.summary} onChange={set('summary')} />
          </div>
          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label>类别 · 必填</label>
              <select className="input" value={form.category} onChange={set('category')} required>
                <option value="">请选择</option>
                {(config?.categories ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>成色 · 必填</label>
              <select className="input" value={form.condition} onChange={set('condition')} required>
                <option value="">请选择</option>
                {CONDITIONS.map((c) => (
                  <option key={c} value={c}>{c}</option>
                ))}
              </select>
            </div>
          </div>
          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label>品牌</label>
              <input className="input" value={form.brand} onChange={set('brand')} placeholder="没有就留空" />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>颜色</label>
              <input className="input" value={form.color} onChange={set('color')} placeholder="没有就留空" />
            </div>
          </div>
        </div>

        <div className="panel-title">02 价格与自提</div>
        <div className="card">
          <div className="field">
            <label>价格（美元）· 你自己定</label>
            <input className="input" type="number" min="1" step="1" value={form.price}
                   onChange={set('price')} placeholder="比如 45" required />
          </div>
          <div className="field">
            <label>自提地点</label>
            <select className="input" value={form.location} onChange={set('location')} required>
              <option value="">选择一个预设地点</option>
              {(config?.locations ?? []).map((l) => <option key={l.name} value={l.name}>{l.name}</option>)}
            </select>
          </div>
          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label>可自提时间（从）</label>
              <input className="input" value={form.timeStart} onChange={set('timeStart')} placeholder="周六 10:00" />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label>（到）</label>
              <input className="input" value={form.timeEnd} onChange={set('timeEnd')} placeholder="周日 18:00" />
            </div>
          </div>
        </div>

        <button className="btn block" disabled={submitting || uploading}>
          {submitting ? <span className="spin" /> : '发布商品'}
        </button>
        <div className="mini muted" style={{ padding: '2px 2px 16px' }}>
          提交后商品会立即入库并建立索引（向量 + BM25），马上就能被搜到。
        </div>
    </form>
  )
}
