import { useEffect, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'

/**
 * Real map (Leaflet + OpenStreetMap tiles, no API key needed).
 * Initial view = the lat/lng bounding box of every location that has items
 * in the database (fitBounds); pins are still custom divIcons (count bubble +
 * location name), and the popover is rendered by React, recomputing container
 * coordinates live as the map pans/zooms.
 */

const POP_W = 336   // popover size, used for flip decisions (keep in sync with the .pin-pop CSS)
const POP_H = 380

const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

function pinHtml(loc, { selected, hit }) {
  const cls = [
    'lpin',
    loc.items.length === 0 ? 'empty' : '',
    selected ? 'selected' : '',
    hit ? 'hit' : '',
  ].filter(Boolean).join(' ')
  return `<div class="${cls}">` +
    (hit ? '<span class="pin-ring"></span>' : '') +
    `<span class="pin-bubble">${loc.items.length}</span>` +
    `<span class="pin-label">${esc(loc.name)}</span>` +
    '</div>'
}

export default function CampusMap({ locations, userPoint, selectedName, hitNames, onSelect, popover }) {
  const divRef = useRef(null)
  const mapRef = useRef(null)
  const markersRef = useRef(new Map())   // name → L.Marker
  const userMarkerRef = useRef(null)
  const fittedRef = useRef(false)
  const [popPos, setPopPos] = useState(null)   // container pixel coords of the selected pin

  // props go into refs so marker callbacks always read the latest values
  const selectedRef = useRef(selectedName)
  selectedRef.current = selectedName
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect
  const locationsRef = useRef(locations)
  locationsRef.current = locations

  // ---------- Initialize the map (once) ----------
  useEffect(() => {
    const map = L.map(divRef.current, {
      zoomControl: false,
      minZoom: 12,
      maxZoom: 19,
    })
    L.control.zoom({ position: 'bottomright' }).addTo(map)   // top-left is taken by the search panel
    // Same-origin on purpose: nginx (and the Vite dev proxy) forward /tiles/ to
    // OpenStreetMap and cache the result. Visitors in mainland China often can't
    // load tile.openstreetmap.org directly, but they can always reach this server.
    L.tileLayer('/tiles/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map)
    map.setView([39.33, -76.6205], 15)   // JHU Homewood fallback view (fitBounds overrides once data arrives)
    map.on('click', () => onSelectRef.current(null))   // clicking empty space closes the popover
    mapRef.current = map
    return () => { map.remove(); mapRef.current = null; markersRef.current.clear(); fittedRef.current = false }
  }, [])

  // ---------- First data batch: fit bounds around locations that have items ----------
  useEffect(() => {
    const map = mapRef.current
    if (!map || fittedRef.current || !locations?.length) return
    const withItems = locations.filter((l) => l.items.length > 0)
    const pts = (withItems.length ? withItems : locations).map((l) => [l.lat, l.lng])
    const bounds = L.latLngBounds(pts)
    map.fitBounds(bounds.pad(0.12))
    map.setMaxBounds(bounds.pad(1.0))   // allow modest panning, but don't let it drift to the ends of the earth
    fittedRef.current = true
  }, [locations])

  // ---------- Sync pins ----------
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const seen = new Set()
    locations.forEach((loc) => {
      seen.add(loc.name)
      const icon = L.divIcon({
        className: 'lpin-anchor',
        html: pinHtml(loc, { selected: selectedName === loc.name, hit: hitNames?.has(loc.name) }),
        iconSize: null,
        iconAnchor: [0, 0],
      })
      let m = markersRef.current.get(loc.name)
      if (!m) {
        m = L.marker([loc.lat, loc.lng], { icon, keyboard: true, title: loc.name })
        m.on('click', () => {
          const name = selectedRef.current === loc.name ? null : loc.name
          onSelectRef.current(name)
        })
        m.addTo(map)
        markersRef.current.set(loc.name, m)
      } else {
        m.setIcon(icon)
      }
      // selected / hit pins float above their peers
      m.setZIndexOffset(selectedName === loc.name ? 1000 : hitNames?.has(loc.name) ? 500 : 0)
    })
    markersRef.current.forEach((m, name) => {
      if (!seen.has(name)) { m.remove(); markersRef.current.delete(name) }
    })
  }, [locations, selectedName, hitNames])

  // ---------- "My location" green dot ----------
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    userMarkerRef.current?.remove()
    userMarkerRef.current = null
    if (userPoint) {
      userMarkerRef.current = L.circleMarker([userPoint.lat, userPoint.lng], {
        radius: 7, color: '#fff', weight: 3, fillColor: '#1f9254', fillOpacity: 1,
      }).bindTooltip('我的位置').addTo(map)
    }
  }, [userPoint])

  // ---------- Popover follows the selected pin (recompute container coords on pan/zoom) ----------
  useEffect(() => {
    const map = mapRef.current
    if (!map) return
    const update = () => {
      const loc = locationsRef.current.find((l) => l.name === selectedRef.current)
      if (!loc) return setPopPos(null)
      const pt = map.latLngToContainerPoint([loc.lat, loc.lng])
      setPopPos({ x: pt.x, y: pt.y })
    }
    if (selectedName) {
      const loc = locationsRef.current.find((l) => l.name === selectedName)
      if (loc) map.panInside([loc.lat, loc.lng], { padding: [80, 80] })
    }
    update()
    map.on('move', update)
    map.on('zoomend', update)
    return () => { map.off('move', update); map.off('zoomend', update) }
  }, [selectedName, locations])

  // Popover flip: open toward the other side when close to the right/bottom edge
  const box = divRef.current
  const flipX = popPos && box && popPos.x > box.clientWidth - (POP_W + 60)
  const flipY = popPos && box && popPos.y > box.clientHeight - POP_H * 0.7

  return (
    <div className="map-stage">
      <div ref={divRef} className="map-bg" />
      <div className="map-hint">标记上的数字 = 该地点在售商品数 · 点击标记查看</div>
      {popPos && popover && (
        <div
          className="pin-pop"
          style={{
            left: popPos.x,
            top: popPos.y,
            transform: [
              flipX ? 'translateX(calc(-100% - 26px))' : 'translateX(26px)',
              flipY ? 'translateY(-85%)' : 'translateY(-24px)',
            ].join(' '),
          }}
          role="dialog"
          aria-label={`${selectedName} 的在售商品`}
        >
          {popover}
        </div>
      )}
    </div>
  )
}
