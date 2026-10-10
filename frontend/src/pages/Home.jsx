import { useState } from 'react'
import BrowseBoard from '../components/BrowseBoard'
import HomeGlobe from '../components/HomeGlobe'

/**
 * Home: a compact hero over the browse board. The board is the point of the
 * page — a buyer who knows what a bookshelf looks like should not have to
 * describe one to a chat box first — so the hero stays one screen-third tall
 * and the globe shrinks to a corner button into the map workbench.
 */
export default function Home({ config, onGo, onPickCategory, onChanged, requireAccount }) {
  // The board owns the category filter; Home only mirrors it so the globe can
  // open the map already narrowed to whatever the buyer is browsing.
  const [boardCategory, setBoardCategory] = useState(null)

  return (
    <div className="page-scroll">
      <div className="container">
        <section className="home-hero">
          <div className="home-hero-text">
            <h1 className="headline">
              <span>就近淘到手。</span>
              <span>随手卖出去。</span>
            </h1>
            <p className="subheadline">
            看看身边同学正在出的闲置，按价格、成色和你愿意走多远来筛——
            或者直接跟地图说你想要什么，让它替你找。
            </p>

            <div className="hero-actions">
              <button className="hbtn hbtn-primary" onClick={() => onGo('find')}>我要找东西</button>
              <button className="hbtn hbtn-glass" onClick={() => onGo('sell')}>我要卖东西</button>
            </div>
          </div>

          {/* The hero globe, reduced to what it always really was: the way into
              the map. No pins at this size — the button is the affordance, and
              it is the only "view on the map" control on the page. */}
          <button className="globe-btn"
                  onClick={() => (boardCategory ? onPickCategory?.(boardCategory) : onGo('find'))}
                  title="打开校园地图" aria-label="在地图上查看这些商品">
            <HomeGlobe categories={config?.categories} compact />
            <span className="globe-btn-label">在地图上看</span>
          </button>
        </section>

        <BrowseBoard config={config} onChanged={onChanged} onCategoryChange={setBoardCategory}
              requireAccount={requireAccount} />
      </div>
    </div>
  )
}
