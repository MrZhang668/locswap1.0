import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import ItemCard from './ItemCard'
import { Empty } from './common'

/**
 * The home board: browse everything on sale with explicit controls — the
 * counterpart to the map workbench's "describe what you want" search. Every
 * filter is resolved server-side by /api/items/browse, so the count and the
 * grid can never drift apart.
 */

// Same spelling and order as the backend's CONDITIONS (config.CONDITION_DICT
// keys) — best first, which is also the order the "成色最好" sort uses.
const CONDITIONS = ['全新', '几乎全新', '成色很好', '成色良好', '成色一般']

// Campus distances: the whole catalogue sits inside a few km, so anything past
// 5 km means the same thing as "any".
const DISTANCES = [
  { value: '', label: '不限距离' },
  { value: 0.5, label: '0.5 公里以内' },
  { value: 1, label: '1 公里以内' },
  { value: 2, label: '2 公里以内' },
  { value: 3, label: '3 公里以内' },
  { value: 5, label: '5 公里以内' },
]

const SORTS = [
  { value: 'newest', label: '最新发布' },
  { value: 'price_asc', label: '价格从低到高' },
  { value: 'price_desc', label: '价格从高到低' },
  { value: 'distance', label: '距离最近' },
  { value: 'condition', label: '成色最好' },
]

const PAGE = 24

const EMPTY_QUERY = {
  q: '',
  category: '',
  subcategory: '',
  min_price: '',
  max_price: '',
  conditions: [],
  max_distance_km: '',
  sort: 'newest',
}

const isDefault = (query) =>
  !query.q && !query.category && !query.subcategory && query.min_price === '' && query.max_price === ''
  && query.conditions.length === 0 && query.max_distance_km === '' && query.sort === 'newest'

export default function BrowseBoard({ config, onChanged, onCategoryChange, requireAccount }) {
  const [query, setQuery] = useState(EMPTY_QUERY)
  const [qText, setQText] = useState('')          // the raw box; query.q is its debounced twin
  const [offset, setOffset] = useState(0)
  const [rows, setRows] = useState([])
  const [total, setTotal] = useState(0)
  const [distanceAvailable, setDistanceAvailable] = useState(true)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [reserving, setReserving] = useState(null)
  // Guards against an out-of-order response overwriting a newer one: only the
  // reply to the most recent request is allowed to land.
  const seq = useRef(0)

  // Any filter change restarts paging — appending page 2 of the old filters to
  // page 1 of the new ones is the classic way to show items that match neither.
  const update = (patch) => {
    setQuery((prev) => ({ ...prev, ...patch }))
    setOffset(0)
  }

  const toggleCondition = (c) => {
    const on = query.conditions.includes(c)
    update({ conditions: on ? query.conditions.filter((x) => x !== c) : [...query.conditions, c] })
  }

  // Typing must not fire one request per keystroke
  useEffect(() => {
    if (qText === query.q) return undefined
    const t = setTimeout(() => update({ q: qText }), 260)
    return () => clearTimeout(t)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qText])

  useEffect(() => {
    const id = ++seq.current
    setLoading(true)
    setError('')
    api
      .browse({
        q: query.q,
        category: query.category,
        subcategory: query.subcategory,
        min_price: query.min_price,
        max_price: query.max_price,
        condition: query.conditions,
        max_distance_km: query.max_distance_km,
        sort: query.sort,
        limit: PAGE,
        offset,
      })
      .then((res) => {
        if (id !== seq.current) return
        setRows((prev) => (offset === 0 ? res.items : [...prev, ...res.items]))
        setTotal(res.total)
        setDistanceAvailable(res.distance_available)
        setLoading(false)
      })
      .catch((e) => {
        if (id !== seq.current) return
        setError(e.message)
        setLoading(false)
      })
  }, [query, offset])

  const reserve = async (card) => {
    // Reserving is the moment an account is actually needed, and the first time
    // a visitor is asked for one. Guarded here rather than by hiding the button:
    // a Reserve they can see and press is what makes signing in worth doing.
    if (!requireAccount?.('预订这件商品', () => true)) return
    setReserving(card.item_id)
    setError('')
    try {
      const updated = await api.reserve(card.item_id)
      setRows((prev) => prev.map((c) => (c.item_id === updated.item_id ? { ...c, ...updated } : c)))
      onChanged?.()
    } catch (e) {
      setError(e.message)
    } finally {
      setReserving(null)
    }
  }

  const clearAll = () => {
    setQText('')
    setQuery(EMPTY_QUERY)
    setOffset(0)
    onCategoryChange?.(null)
  }

  const clean = isDefault(query) && !qText
  const priceInverted =
    query.min_price !== '' && query.max_price !== ''
    && Number(query.min_price) > Number(query.max_price)

  return (
    <section className="board" aria-label="浏览商品">
      <div className="board-head">
        <h2>最新在售</h2>
      </div>

      <div className="filterbar">
        <div className="fb-field grow">
          <label htmlFor="fb-q">搜索</label>
          <input id="fb-q" className="input" placeholder="你在找什么？"
                 value={qText} maxLength={120} onChange={(e) => setQText(e.target.value)} />
        </div>

        <div className="fb-field">
          <label htmlFor="fb-cat">类别</label>
          <select id="fb-cat" className="input" value={query.category}
                  onChange={(e) => {
                    // 换大类就清掉子类目：旧的那个不属于新的大类，留着只会筛出空板。
                    update({ category: e.target.value, subcategory: '' })
                    onCategoryChange?.(e.target.value || null)
                  }}>
            <option value="">全部</option>
            {(config?.categories ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>

        <div className="fb-field">
          <label htmlFor="fb-sub">子类目</label>
          <select id="fb-sub" className="input" value={query.subcategory}
                  disabled={!query.category}
                  title={query.category ? undefined : '先选一个类别'}
                  onChange={(e) => update({ subcategory: e.target.value })}>
            <option value="">全部</option>
            {(config?.subcategories?.[query.category] ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>

        <div className="fb-field narrow">
          <label htmlFor="fb-min">最低价 $</label>
          <input id="fb-min" className="input" type="number" min="0" step="1" placeholder="0"
                 value={query.min_price} onChange={(e) => update({ min_price: e.target.value })} />
        </div>

        <div className="fb-field narrow">
          <label htmlFor="fb-max">最高价 $</label>
          <input id="fb-max" className="input" type="number" min="0" step="1" placeholder="不限"
                 value={query.max_price} onChange={(e) => update({ max_price: e.target.value })} />
        </div>

        <div className="fb-field">
          <label htmlFor="fb-dist">距离</label>
          <select id="fb-dist" className="input" value={query.max_distance_km}
                  disabled={!distanceAvailable}
                  title={distanceAvailable ? undefined : '请先在个人资料里设置你的位置'}
                  onChange={(e) => update({ max_distance_km: e.target.value })}>
            {DISTANCES.map((d) => <option key={d.label} value={d.value}>{d.label}</option>)}
          </select>
        </div>

        <div className="fb-field">
          <label htmlFor="fb-sort">排序</label>
          <select id="fb-sort" className="input" value={query.sort}
                  onChange={(e) => update({ sort: e.target.value })}>
            {SORTS.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </select>
        </div>

        <div className="fb-field conditions">
          <label>成色</label>
          <div className="chiprow">
            {CONDITIONS.map((c) => (
              <button key={c} type="button"
                      className={`chip ${query.conditions.includes(c) ? 'on' : ''}`}
                      aria-pressed={query.conditions.includes(c)}
                      onClick={() => toggleCondition(c)}>
                {c}
              </button>
            ))}
          </div>
        </div>

        <button className="btn plain sm fb-clear" onClick={clearAll} disabled={clean}>清空</button>
      </div>

      {!distanceAvailable && (
        <div className="notice info" style={{ marginBottom: 12 }}>
          没有设置位置之前，距离筛选不可用——去{' '}
          <span className="strong">个人资料</span>里选一个地点。
        </div>
      )}
      {priceInverted && (
        <div className="notice warn" style={{ marginBottom: 12 }}>
          最低价高于最高价，这样不会有任何结果。把两个数对调一下就好。
        </div>
      )}
      {error && <div className="notice err" style={{ marginBottom: 12 }}>{error}</div>}

      <div className="board-count">
        {loading && rows.length === 0 ? '加载中…' : `共 ${total} 件商品`}
      </div>

      {rows.length === 0 && !loading ? (
        <Empty>
          当前这些筛选条件下没有商品。可以把价格范围放宽一点，
          或者去地图搜索里描述你想要什么，我们会帮你留意。
        </Empty>
      ) : (
        <div className="board-grid">
          {rows.map((card) => (
            <div key={card.item_id} onClick={() => api.clickItem(card.item_id)}>
              <ItemCard card={card} onReserve={reserve} busy={reserving === card.item_id} />
            </div>
          ))}
        </div>
      )}

      {rows.length < total && (
        <div className="board-more">
          <button className="btn plain" disabled={loading} onClick={() => setOffset(rows.length)}>
            {loading ? '加载中…' : `再看更多（还有 ${total - rows.length} 件）`}
          </button>
        </div>
      )}
    </section>
  )
}
