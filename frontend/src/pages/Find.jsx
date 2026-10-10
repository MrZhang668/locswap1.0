import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import CampusMap from '../components/CampusMap'
import ItemCard from '../components/ItemCard'
import { useAsync } from '../components/common'

const SUGGESTS = ['150 以内的二手自行车', '80 以内的黑色耳机', '微积分入门教材', '夏天宿舍用的降温的东西', '打球用的球拍']

const DECISION_LABEL = {
  hit: '命中',
  structured_fallback: '结构化兜底',
  filter_fallback: '条件兜底',
  low_confidence: '低置信',
  clarify: '追问',
  empty: '无结果',
  privacy_refusal: '隐私拒答',
}

/**
 * Search page: desktop map workbench.
 * Big central map (pins = pickup spots), natural-language search panel on the
 * left, category filter panel on the right. The three stay in sync: filters
 * drive pin counts and popovers, and search results highlight their pins.
 */
export default function Find({ user, config, onChanged, initialCategory, requireAccount }) {
  // ---------- Chat retrieval (same pipeline as before) ----------
  const [turns, setTurns] = useState([
    { role: 'assistant', content: '你想找点什么？用大白话说就行，比如「150 以内的二手自行车，离宿舍近一点」。找到之后我会在地图上标出它们的位置。' },
  ])
  const [blocks, setBlocks] = useState([]) // card groups hanging off each AI reply
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [clarifyRound, setClarifyRound] = useState(0)
  const [reserving, setReserving] = useState(null)
  const [subscribing, setSubscribing] = useState(null)
  const [subscribed, setSubscribed] = useState({})
  // Free-text "here's what I actually want" composer under each result block,
  // keyed by block index so an older block keeps its own draft.
  const [wantDraft, setWantDraft] = useState({})
  const [wantSaved, setWantSaved] = useState({})
  const [wantBusy, setWantBusy] = useState(null)
  const [error, setError] = useState('')
  const endRef = useRef(null)

  // ---------- Map workbench ----------
  const map = useAsync(() => api.mapItems(), [])
  const [cats, setCats] = useState(() => new Set(initialCategory ? [initialCategory] : [])) // empty = all categories
  const [selected, setSelected] = useState(null)    // selected pin (location name)
  const [leftOpen, setLeftOpen] = useState(true)
  const [rightOpen, setRightOpen] = useState(true)

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [turns, blocks])

  // Esc closes the pin popover (keyboard accessibility)
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') setSelected(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const rawLocations = map.data?.locations ?? []

  const matchCat = (card) => cats.size === 0 || (card.category && cats.has(card.category))

  // Location data after category filtering (used by both pin counts and popovers)
  const locations = useMemo(
    () => rawLocations.map((l) => ({ ...l, items: l.items.filter(matchCat) })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rawLocations, cats],
  )

  // item_id → location name (drives pin highlighting and card-to-pin jumps)
  const itemLoc = useMemo(() => {
    const m = new Map()
    rawLocations.forEach((l) => l.items.forEach((it) => m.set(it.item_id, l.name)))
    return m
  }, [rawLocations])

  // Item count per category (right panel counters)
  const catCounts = useMemo(() => {
    const m = new Map()
    rawLocations.forEach((l) => l.items.forEach((it) => {
      if (it.category) m.set(it.category, (m.get(it.category) ?? 0) + 1)
    }))
    return m
  }, [rawLocations])

  // Items hit by the latest search → highlight their pins
  const lastCards = blocks.length ? blocks[blocks.length - 1].cards : []
  const hitNames = useMemo(() => {
    const s = new Set()
    lastCards.filter(matchCat).forEach((c) => {
      const name = itemLoc.get(c.item_id)
      if (name) s.add(name)
    })
    return s
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [lastCards, itemLoc, cats])

  const userPoint = useMemo(() => {
    const mine = (config?.locations ?? []).find((l) => l.name === user?.location_name)
    return mine ? { lat: mine.lat, lng: mine.lng } : null
  }, [config, user?.location_name])

  const toggleCat = (c) => {
    setCats((prev) => {
      const next = new Set(prev)
      if (next.has(c)) next.delete(c)
      else next.add(c)
      return next
    })
  }

  const send = async (text) => {
    const message = (text ?? input).trim()
    if (!message || busy) return
    setInput('')
    setError('')
    const nextTurns = [...turns, { role: 'user', content: message }]
    setTurns(nextTurns)
    setBusy(true)
    try {
      // Only the latest 4 turns (token control); the backend trims again
      const history = nextTurns.slice(-8).map((t) => ({ role: t.role, content: t.content }))
      const res = await api.chat(message, history, clarifyRound)
      setClarifyRound(res.clarify_round)
      setTurns((prev) => [...prev, { role: 'assistant', content: res.reply }])
      setBlocks((prev) => [
        ...prev,
        {
          at: nextTurns.length,
          cards: res.cards,
          debug: res.debug,
          decision: res.decision,
          confidence: res.confidence,
          wantId: res.want_id ?? null,   // E2 already saved this query as a want — see "Notify me" below
        },
      ])
    } catch (e) {
      setError(e.message)
      setTurns((prev) => [...prev, { role: 'assistant', content: `出错了：${e.message}` }])
    } finally {
      setBusy(false)
    }
  }

  // "Notify me": E2 already persisted this want, so this just upgrades it to an
  // explicit subscription — no re-parsing, no LLM call, zero marginal cost.
  const subscribe = async (wantId) => {
    setSubscribing(wantId)
    try {
      await api.subscribeWant(wantId)
      setSubscribed((prev) => ({ ...prev, [wantId]: true }))
    } catch (e) {
      setError(e.message)
    } finally {
      setSubscribing(null)
    }
  }

  // The results missed → the buyer states the need in their own words. This is
  // a real standing want (parsed by the same zero-LLM rule parser as a search),
  // not feedback: it stays on file and is what sellers see as an anonymized
  // candidate card.
  const saveWant = async (blockAt) => {
    const text = (wantDraft[blockAt] ?? '').trim()
    if (!text || wantBusy !== null) return
    setWantBusy(blockAt)
    setError('')
    try {
      await api.createWant(text)
      setWantSaved((prev) => ({ ...prev, [blockAt]: text }))
      setWantDraft((prev) => ({ ...prev, [blockAt]: '' }))
    } catch (e) {
      setError(e.message)
    } finally {
      setWantBusy(null)
    }
  }

  const reserve = async (card) => {
    // Reserving is the moment an account is actually needed, and the first time
    // a visitor is asked for one. Guarded here rather than by hiding the button:
    // a Reserve they can see and press is what makes signing in worth doing.
    if (!requireAccount?.('预订这件商品', () => true)) return
    setReserving(card.item_id)
    setError('')
    try {
      const updated = await api.reserve(card.item_id)
      setBlocks((prev) =>
        prev.map((b) => ({
          ...b,
          cards: b.cards.map((c) => (c.item_id === updated.item_id ? { ...c, ...updated } : c)),
        })),
      )
      map.reload()
      onChanged?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setReserving(null)
    }
  }

  // Clicking a result card → locate its pin on the map (click tracking still fires)
  const focusCard = (card) => {
    api.clickItem(card.item_id)
    const name = itemLoc.get(card.item_id)
    if (name) setSelected(name)
  }

  const selectedLoc = locations.find((l) => l.name === selected)

  return (
    <div className="workbench">
      <CampusMap
        locations={locations}
        userPoint={userPoint}
        selectedName={selected}
        hitNames={hitNames}
        onSelect={setSelected}
        popover={selectedLoc && (
          <>
            <div className="wb-head">
              <span className="t">{selectedLoc.name} · {selectedLoc.items.length} 件在售</span>
              <button className="wb-collapse" onClick={() => setSelected(null)} aria-label="关闭浮层">×</button>
            </div>
            <div className="wb-body">
              {selectedLoc.items.length === 0 ? (
                <div className="tiny muted" style={{ padding: '8px 4px' }}>
                  这个地点暂时没有{cats.size > 0 ? '符合筛选条件的' : ''}在售商品。
                </div>
              ) : (
                selectedLoc.items.map((c) => (
                  <div key={c.item_id} className="pop-item" onClick={() => api.clickItem(c.item_id)}>
                    {c.photo_url ? <img className="thumb" src={c.photo_url} alt="" /> : <div className="thumb" />}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="nm">{c.name}</div>
                      <div className="row" style={{ gap: 5, marginTop: 3, flexWrap: 'wrap' }}>
                        {c.category && <span className="pill">{c.category}</span>}
                        {c.distance_km != null && <span className="pill">距离 {c.distance_km} 公里</span>}
                        {c.status === 'reserved' && <span className="pill warn">{c.reserved_count} 人已预订</span>}
                      </div>
                      <div className="row between" style={{ marginTop: 4 }}>
                        <span className="price">${Number(c.price).toFixed(0)}</span>
                        {c.reserved_by_me ? (
                          <span className="pill brand">你排在第 {c.my_rank} 位</span>
                        ) : (
                          <button className="btn sm" disabled={reserving === c.item_id}
                                  onClick={(e) => { e.stopPropagation(); reserve(c) }}>
                            {reserving === c.item_id ? '预订中…' : '预订'}
                          </button>
                        )}
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </>
        )}
      />

      {/* ---------- Left: natural-language search panel ---------- */}
      {leftOpen ? (
        <section className="wb-panel left" aria-label="自然语言搜索">
          <div className="wb-head">
            <span className="t">搜索</span>
            <button className="wb-collapse" onClick={() => setLeftOpen(false)}
                    aria-label="收起搜索面板" title="收起">‹</button>
          </div>
          <div className="wb-body">
            <div className="wb-chat">
              {turns.map((turn, i) => {
                const block = blocks.find((b) => b.at === i)
                return (
                  <div key={i} className="col" style={{ gap: 10 }}>
                    <div className={`bubble ${turn.role === 'user' ? 'me' : 'ai'}`}>{turn.content}</div>
                    {block?.cards?.length > 0 && (
                      <div className="col" style={{ gap: 8 }}>
                        {block.cards.map((card) => (
                          <div key={card.item_id} onClick={() => focusCard(card)}
                               title="点击在地图上定位" style={{ cursor: 'pointer' }}>
                            <ItemCard card={card} onReserve={reserve}
                                      busy={reserving === card.item_id} />
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Didn't find it → let the buyer turn this query into a standing subscription. */}
                    {block?.wantId && (
                      subscribed[block.wantId] ? (
                        <div className="notice info">记下了，等它到货第一时间通知你。</div>
                      ) : (
                        <button className="btn ghost sm" style={{ alignSelf: 'flex-start' }}
                                disabled={subscribing === block.wantId}
                                onClick={() => subscribe(block.wantId)}>
                          {subscribing === block.wantId ? '设置中…' : '🔔 有货通知我'}
                        </button>
                      )
                    )}
                    {/* Not what they were after → let the buyer say it in their own
                        words. Shown under every result block, hit or miss: a
                        result that "looks right" but isn't is exactly the case
                        the automatic capture (E2) never sees. */}
                    {block && (
                      wantSaved[block.at] ? (
                        <div className="notice info">
                          已保存：「{wantSaved[block.at]}」——它在<span className="strong">购物车 → 等着的东西</span>里。
                          有类似东西的卖家可以联系到你，但看不到你是谁。
                        </div>
                      ) : (
                        <form className="col" style={{ gap: 6 }}
                              onSubmit={(e) => { e.preventDefault(); saveWant(block.at) }}>
                          <div className="mini muted">不太对？说说你真正想要的是什么：</div>
                          <div className="row" style={{ gap: 6 }}>
                            <input className="inp" style={{ flex: 1 }} maxLength={300}
                                   placeholder="比如：200 以内的 26 寸山地车，离 Homewood 近一点"
                                   value={wantDraft[block.at] ?? ''}
                                   onChange={(e) => setWantDraft((prev) => ({ ...prev, [block.at]: e.target.value }))} />
                            <button className="btn ghost sm" type="submit"
                                    disabled={wantBusy === block.at || !(wantDraft[block.at] ?? '').trim()}>
                              {wantBusy === block.at ? '保存中…' : '保存需求'}
                            </button>
                          </div>
                        </form>
                      )
                    )}
                    {block && (
                      <details className="debug-wrap">
                        <summary>
                          检索详情：{DECISION_LABEL[block.decision] ?? block.decision}
                          {block.decision !== 'privacy_refusal' && ` · 置信度 ${block.confidence}`}
                        </summary>
                        <pre className="debug">{JSON.stringify(block.debug, null, 2)}</pre>
                      </details>
                    )}
                  </div>
                )
              })}

              {turns.length <= 1 && (
                <div className="suggests">
                  {SUGGESTS.map((s) => (
                    <button key={s} onClick={() => send(s)}>{s}</button>
                  ))}
                </div>
              )}

              {busy && <div className="bubble ai muted">搜索中…</div>}
              {error && <div className="notice err">{error}</div>}
              <div ref={endRef} />
            </div>
          </div>
          <form className="composer" onSubmit={(e) => { e.preventDefault(); send() }}>
            <input value={input} onChange={(e) => setInput(e.target.value)}
                   placeholder="说说你想找什么…" disabled={busy} />
            <button className="btn" disabled={busy || !input.trim()}>发送</button>
          </form>
        </section>
      ) : (
        <button className="wb-open left" onClick={() => setLeftOpen(true)}>› 搜索面板</button>
      )}

      {/* ---------- Right: category filter panel ---------- */}
      {rightOpen ? (
        <section className="wb-panel right" aria-label="类别筛选">
          <div className="wb-head">
            <span className="t">类别筛选</span>
            <button className="wb-collapse" onClick={() => setRightOpen(false)}
                    aria-label="收起筛选面板" title="收起">›</button>
          </div>
          <div className="wb-body" style={{ padding: 8 }}>
            <button className={`filter-item ${cats.size === 0 ? 'on' : ''}`}
                    onClick={() => setCats(new Set())}>
              <span className="box">{cats.size === 0 ? '✓' : ''}</span>
              全部类别
              <span className="count">{[...catCounts.values()].reduce((a, b) => a + b, 0)}</span>
            </button>
            {(config?.categories ?? []).map((c) => (
              <button key={c} className={`filter-item ${cats.has(c) ? 'on' : ''}`}
                      onClick={() => toggleCat(c)}>
                <span className="box">{cats.has(c) ? '✓' : ''}</span>
                {c}
                <span className="count">{catCounts.get(c) ?? 0}</span>
              </button>
            ))}
            {map.error && <div className="notice err" style={{ margin: 8 }}>{map.error}</div>}
          </div>
        </section>
      ) : (
        <button className="wb-open right" onClick={() => setRightOpen(true)}>筛选 ‹</button>
      )}
    </div>
  )
}
