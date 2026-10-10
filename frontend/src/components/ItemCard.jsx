import { StatusPill } from './common'

/**
 * Buyer-side item card — fields map 1:1 to the backend ItemCard whitelist.
 * It shows (and receives) no personal info: the seller's email only comes back
 * from the cart endpoint.
 */
export default function ItemCard({ card, onReserve, busy, preview }) {
  const reserved = card.reserved_by_me
  return (
    <div className="item">
      {card.photo_url ? (
        <img className="thumb" src={card.photo_url} alt="" />
      ) : (
        <div className="thumb" />
      )}
      <div className="body">
        <div className="name">{card.name || '（标题待补充）'}</div>
        <div className="summary">{card.summary || '（一句话摘要待补充）'}</div>
        <div className="meta">
          {card.category && <span className="pill">{card.category}</span>}
          {card.condition && <span className="pill">{card.condition}</span>}
          {card.distance_km !== null && card.distance_km !== undefined && (
            <span className="pill">距离 {card.distance_km} 公里</span>
          )}
          {card.status === 'reserved' && (
            <span className="pill warn">{card.reserved_count} 人已预订</span>
          )}
        </div>
        {/* No inline margin here: the board grid pins this row to the bottom of
            the card with margin-top:auto, and an inline style would win over it. */}
        <div className="row between">
          <div className="price">
            ${Number(card.price ?? 0).toFixed(0)}
          </div>
          {preview ? (
            <StatusPill status={card.status || 'selling'} />
          ) : reserved ? (
            <span className="pill brand">你排在第 {card.my_rank} 位</span>
          ) : (
            <button className="btn sm" disabled={busy} onClick={() => onReserve(card)}>
              {busy ? '预订中…' : '预订'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
