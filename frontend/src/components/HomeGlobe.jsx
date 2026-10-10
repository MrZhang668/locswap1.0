import { useEffect, useRef } from 'react'
import * as THREE from 'three'

/**
 * Home visual: a slowly spinning, draggable Earth with floating category chips.
 * The chips are plain DOM (not sprites) so they stay crisp and clickable; every frame we
 * project their globe-surface position into container pixels and hide the ones facing away.
 *
 * `compact` is the corner-button variant: same sphere, no pins and no drag. At ~110px a
 * 34x46 pin would be half the planet, and drag-to-spin would fight the click that the
 * button exists for — so both are dropped rather than scaled down.
 */

const GLOBE_TEXTURE = '/earth-texture.jpg'
const CLOUD_TEXTURE = '/earth-clouds.png'
const RADIUS = 2.2
// Axial tilt, measured from screen-up (the direction the UI text stands in). The group is
// rotated about Z, which is the axis pointing at the camera, so this value *is* the angle
// between the spin axis and vertical on screen. Pin placement follows via tiltQuat.
const TILT_DEG = 33.3
const TILT_RAD = (TILT_DEG * Math.PI) / 180

// Chip placement is decorative — spread over the visible hemisphere, not real geography.
// `match` is looked up against the backend category list so chips never advertise a
// category the catalogue doesn't have.
// One head colour for every category; the glyph is what tells them apart. The brand blue's
// hue held, but pulled back from full saturation so it reads as a painted object rather than
// a lit pixel — still light enough to separate from the navy ocean underneath.
const PIN_COLOR = '#5479d6'

// `match` is compared against the category names the backend sends, so these
// are the Chinese canonical values from config.CATEGORY_DICT — an English name
// here silently drops the pin instead of erroring.
const PIN_LAYOUT = [
  { match: '图书与教材', key: 'books', lat: 35, lon: -80 },
  { match: '电子产品', key: 'laptop', lat: 50, lon: 10 },
  { match: '家具与电器', key: 'chair', lat: -20, lon: 30 },
  { match: '服装与鞋靴', key: 'shirt', lat: 25, lon: 120 },
  { match: '乐器', key: 'note', lat: -35, lon: -60 },
  { match: '自行车与滑板车', key: 'bike', lat: 60, lon: -120 },
]

/* Each pin head carries the category's silhouette, drawn white on the ball. Kept to a
   handful of strokes because the head is only 26px across. */
const PIN_GLYPHS = {
  books: '<path d="M8 4.7C6.6 3.5 4.6 3.2 2.4 3.6v8.3c2.2-.4 4.2-.1 5.6 1.1z" fill="#fff" stroke="none"/>'
       + '<path d="M8 4.7c1.4-1.2 3.4-1.5 5.6-1.1v8.3c-2.2-.4-4.2-.1-5.6 1.1z" fill="#fff" stroke="none" opacity=".78"/>',
  laptop: '<path d="M3.7 4.3h8.6v6H3.7z"/><path d="M1.7 12.3h12.6"/>',
  chair: '<path d="M4.4 3.2h7.2a1.5 1.5 0 0 1 1.5 1.5v3.4H2.9V4.7a1.5 1.5 0 0 1 1.5-1.5z" fill="#fff" stroke="none"/>'
       + '<path d="M2.4 8.4h11.2a1.1 1.1 0 0 1 1.1 1.1v1.8H1.3V9.5a1.1 1.1 0 0 1 1.1-1.1z" fill="#fff" stroke="none"/>'
       + '<path d="M3.2 11.6v1.9M12.8 11.6v1.9" stroke-width="1.7"/>',
  shirt: '<path d="M4.2 3.5 6.7 2.4a2.3 2.3 0 0 0 2.6 0l2.5 1.1 1.4 2.4-2.1 1.2v6.3H4.9V7.1L2.8 5.9z" fill="#fff" stroke="none"/>',
  note: '<path d="M6.6 11.5V3.5l5.9-1.3v7.7" stroke-width="1.6"/>'
      + '<ellipse cx="4.9" cy="11.7" rx="1.9" ry="1.6" fill="#fff" stroke="none"/>'
      + '<ellipse cx="10.8" cy="10.1" rx="1.9" ry="1.6" fill="#fff" stroke="none"/>',
  bike: '<circle cx="4.7" cy="10.6" r="3"/><circle cx="11.3" cy="10.6" r="3"/><path d="M4.7 10.6 7.5 5.9l3.8 4.7"/><path d="M6.3 5.9h2.6"/>',
}

/** Mixes a hex colour toward black, for the shaded underside of the ball. */
function shade(hex, amount = 0.36) {
  const n = parseInt(hex.slice(1), 16)
  const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => Math.round(v * (1 - amount)))
  return `rgb(${c.join(',')})`
}

/** A push-pin: grey needle, glossy category-coloured head. The needle tip is the anchor. */
function pinMarkup({ key }) {
  return `<svg viewBox="0 0 34 46" width="34" height="46" aria-hidden="true" focusable="false">
    <defs>
      <linearGradient id="ndl-${key}" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0" stop-color="#6b7078"/>
        <stop offset=".42" stop-color="#c2c7ce"/>
        <stop offset="1" stop-color="#767b84"/>
      </linearGradient>
      <radialGradient id="head-${key}" cx="34%" cy="27%" r="76%">
        <stop offset="0" stop-color="#fff" stop-opacity=".4"/>
        <stop offset=".4" stop-color="${PIN_COLOR}"/>
        <stop offset="1" stop-color="${shade(PIN_COLOR)}"/>
      </radialGradient>
    </defs>
    <rect x="15.3" y="19" width="3.4" height="26" rx="1.7" fill="url(#ndl-${key})"/>
    <circle cx="17" cy="15" r="13" fill="url(#head-${key})"/>
    <ellipse cx="11.4" cy="8.9" rx="4" ry="2.8" fill="#fff" opacity=".34"/>
    <g transform="translate(9 7)" fill="none" stroke="#fff" stroke-width="1.5"
       stroke-linecap="round" stroke-linejoin="round" opacity=".96">${PIN_GLYPHS[key]}</g>
  </svg>`
}

function latLonToVec3(lat, lon, radius) {
  const phi = (90 - lat) * (Math.PI / 180)
  const theta = (lon + 180) * (Math.PI / 180)
  return new THREE.Vector3(
    -radius * Math.sin(phi) * Math.cos(theta),
    radius * Math.cos(phi),
    radius * Math.sin(phi) * Math.sin(theta),
  )
}

export default function HomeGlobe({ categories, onPick, compact = false }) {
  const wrapRef = useRef(null)
  const canvasRef = useRef(null)
  const pinsFrontRef = useRef(null)
  const pinsBackRef = useRef(null)
  // onPick changes identity on every App render; keep it in a ref so the scene isn't rebuilt
  const pickRef = useRef(onPick)
  pickRef.current = onPick

  const available = Array.isArray(categories) && categories.length ? categories : null
  const pinKey = compact
    ? ''
    : (available ? PIN_LAYOUT.filter((p) => available.includes(p.match)) : PIN_LAYOUT)
      .map((p) => p.match).join('|')

  useEffect(() => {
    const container = wrapRef.current
    const canvas = canvasRef.current
    const frontLayer = pinsFrontRef.current
    const backLayer = pinsBackRef.current
    if (!container || !canvas || !frontLayer || !backLayer) return undefined

    const pinData = compact
      ? []
      : pinKey ? PIN_LAYOUT.filter((p) => pinKey.split('|').includes(p.match)) : PIN_LAYOUT

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    // Pin the colour pipeline: three's defaults have moved between releases and the
    // hero's brightness is tuned against these two settings.
    renderer.toneMapping = THREE.ACESFilmicToneMapping
    renderer.toneMappingExposure = 1.45
    renderer.outputColorSpace = THREE.SRGBColorSpace
    renderer.setSize(container.clientWidth, container.clientHeight)

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(45, container.clientWidth / container.clientHeight, 0.1, 100)
    camera.position.set(0, 0, 7.1)  // far enough that the outer halo shell stays inside the frame
    camera.lookAt(0, 0, 0)

    // Key light sits front-upper-left so the terminator cuts a diagonal across the disc;
    // the blue fill keeps the night side readable instead of a flat black cut-out.
    scene.add(new THREE.AmbientLight('#c6d9ef', 0.55))
    const dirLight = new THREE.DirectionalLight('#ffffff', 3.2)
    dirLight.position.set(-2, 1.2, 6)
    scene.add(dirLight)
    const fillLight = new THREE.DirectionalLight('#3b6ecc', 0.5)
    fillLight.position.set(3, -2, -3)
    scene.add(fillLight)

    const globeGroup = new THREE.Group()
    globeGroup.rotation.z = TILT_RAD
    scene.add(globeGroup)

    const globeGeo = new THREE.SphereGeometry(RADIUS, 64, 64)
    const globeMat = new THREE.MeshStandardMaterial({ color: '#20344f', roughness: 0.6, metalness: 0 })
    const globeMesh = new THREE.Mesh(globeGeo, globeMat)
    globeGroup.add(globeMesh)

    // Cloud shell sits just above the surface and drifts slightly faster than the ground,
    // which is what stops the sphere from reading as a flat painted ball.
    const cloudMat = new THREE.MeshStandardMaterial({
      transparent: true,
      opacity: 0.9,
      depthWrite: false,
      roughness: 1,
      metalness: 0,
    })
    const cloudMesh = new THREE.Mesh(new THREE.SphereGeometry(RADIUS * 1.012, 64, 64), cloudMat)
    cloudMesh.visible = false
    globeGroup.add(cloudMesh)

    const textureLoader = new THREE.TextureLoader()
    const loaded = []
    textureLoader.load(GLOBE_TEXTURE, (tex) => {
      loaded.push(tex)
      tex.colorSpace = THREE.SRGBColorSpace
      globeMat.map = tex
      globeMat.color.set('#ffffff')
      globeMat.needsUpdate = true
    })
    textureLoader.load(CLOUD_TEXTURE, (tex) => {
      loaded.push(tex)
      tex.colorSpace = THREE.SRGBColorSpace
      cloudMat.map = tex
      cloudMat.alphaMap = tex
      cloudMat.needsUpdate = true
      cloudMesh.visible = true
    })

    // Three nested back-faced spheres fake an atmospheric bloom around the rim
    const shells = [
      { r: 2.28, color: '#9fd7ff', opacity: 0.20, seg: 64 },
    ].map(({ r, color, opacity, seg }) => {
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(r, seg, seg),
        new THREE.MeshBasicMaterial({
          color,
          transparent: true,
          opacity,
          side: THREE.BackSide,
          depthWrite: false,
          blending: THREE.AdditiveBlending,
        }),
      )
      globeGroup.add(mesh)
      return mesh
    })

    // Every pin exists twice: once above the globe and once below it. Both copies track the
    // same point; crossfading between them at the limb is what makes a pin slide behind the
    // sphere instead of switching layers in a single frame, which reads as a blink.
    const pins = pinData.map((pin) => {
      const make = (layer, interactive) => {
        const el = document.createElement(interactive ? 'button' : 'div')
        el.className = 'globe-pin'
        if (interactive) {
          el.type = 'button'
          el.title = pin.match
          el.setAttribute('aria-label', pin.match)
          el.addEventListener('click', () => pickRef.current?.(pin.match))
        } else {
          el.setAttribute('aria-hidden', 'true')
        }
        el.innerHTML = pinMarkup(pin)
        layer.appendChild(el)
        return { el, art: el.firstElementChild }
      }
      return {
        front: make(frontLayer, true),
        back: make(backLayer, false),
        localPos: latLonToVec3(pin.lat, pin.lon, RADIUS),
      }
    })

    let isDragging = false
    let prevX = 0
    let rotY = 0
    let velY = 0

    const onDown = (e) => {
      if (e.button !== 0) return
      isDragging = true
      prevX = e.clientX
      velY = 0
      canvas.style.cursor = 'grabbing'
      e.preventDefault()
    }
    const onMove = (e) => {
      if (!isDragging) return
      velY = (e.clientX - prevX) * 0.007
      rotY += velY
      prevX = e.clientX
    }
    const onUp = () => {
      isDragging = false
      canvas.style.cursor = 'grab'
    }
    if (!compact) {
      canvas.style.cursor = 'grab'
      canvas.addEventListener('mousedown', onDown)
      window.addEventListener('mousemove', onMove)
      window.addEventListener('mouseup', onUp)
    }

    // Fade band, in facing units: fully lit by 0.34, and the tail runs well past the limb so a
    // pin that has gone round the back sinks out of sight instead of blinking off.
    const FADE_START = 0.34
    const FADE_END = -0.32
    // Handover band: where the pin stops being in front of the sphere and starts being behind it.
    const HAND_BACK = -0.11
    const HAND_FRONT = 0.07

    const camDir = new THREE.Vector3()
    const tiltQuat = new THREE.Quaternion().setFromEuler(globeGroup.rotation)
    const yAxis = new THREE.Vector3(0, 1, 0)
    const world = new THREE.Vector3()

    let animId = 0
    const animate = () => {
      animId = requestAnimationFrame(animate)

      if (!isDragging) {
        velY *= 0.92
        rotY += velY + 0.0015
      }
      globeMesh.rotation.y = rotY
      cloudMesh.rotation.y = rotY * 1.12
      shells.forEach((m) => { m.rotation.y = rotY })

      camera.getWorldDirection(camDir)
      const cw = container.clientWidth
      const ch = container.clientHeight

      pins.forEach((pin) => {
        const { front, back, localPos } = pin
        world.copy(localPos).applyAxisAngle(yAxis, rotY).applyQuaternion(tiltQuat)

        // facing: +1 dead centre, 0 exactly on the limb, negative once round the back.
        // Instead of a hard cut at the limb, ramp opacity and scale across a band either
        // side of it, so a pin grows out of the horizon and sinks back into it.
        const facing = -world.clone().normalize().dot(camDir)
        const raw = (facing - FADE_END) / (FADE_START - FADE_END)
        const k = Math.min(1, Math.max(0, raw))
        const vis = k * k * (3 - 2 * k)   // smoothstep, so both ends ease instead of ramping linearly

        // Position is updated even while invisible: a pin that fades back in must not jump.
        const projected = world.clone().project(camera)
        // Weight between the copy above the globe and the one below it. The lower copy is
        // occluded by the opaque sphere, so whatever still pokes past the silhouette is all
        // that shows — which is exactly how a pin going over the horizon should behave.
        const hRaw = (facing - HAND_BACK) / (HAND_FRONT - HAND_BACK)
        const h = Math.min(1, Math.max(0, hRaw))
        const w = h * h * (3 - 2 * h)

        const left = `${(projected.x * 0.5 + 0.5) * cw}px`
        const top = `${(-projected.y * 0.5 + 0.5) * ch}px`
        // Scaled about the needle tip, so it shrinks into the surface rather than toward its own middle
        const artScale = `scale(${(0.62 + 0.38 * vis).toFixed(3)})`

        front.el.style.left = left
        front.el.style.top = top
        front.el.style.opacity = (vis * w).toFixed(3)
        front.el.style.pointerEvents = vis * w > 0.55 ? 'auto' : 'none'
        front.art.style.transform = artScale

        back.el.style.left = left
        back.el.style.top = top
        back.el.style.opacity = (vis * (1 - w)).toFixed(3)
        back.art.style.transform = artScale
      })

      renderer.render(scene, camera)
    }
    animate()

    const onResize = () => {
      const nw = container.clientWidth
      const nh = container.clientHeight
      if (!nw || !nh) return
      camera.aspect = nw / nh
      camera.updateProjectionMatrix()
      renderer.setSize(nw, nh)
    }
    window.addEventListener('resize', onResize)

    return () => {
      cancelAnimationFrame(animId)
      canvas.removeEventListener('mousedown', onDown)
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      window.removeEventListener('resize', onResize)
      pins.forEach(({ front, back }) => { front.el.remove(); back.el.remove() })
      shells.forEach((m) => { m.geometry.dispose(); m.material.dispose() })
      globeGeo.dispose()
      globeMat.dispose()
      cloudMesh.geometry.dispose()
      cloudMat.dispose()
      loaded.forEach((t) => t.dispose())
      renderer.dispose()
    }
  }, [pinKey, compact])

  return (
    <div className={`globe-wrap${compact ? ' mini' : ''}`} ref={wrapRef}>
      <div className="globe-aura" aria-hidden="true" />
      <div className="globe-pins back" ref={pinsBackRef} />
      <canvas className="globe-canvas" ref={canvasRef} />
      <div className="globe-pins front" ref={pinsFrontRef} />
    </div>
  )
}
