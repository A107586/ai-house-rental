import { useEffect, useState } from 'react'
import { api } from '../api'

const STATUS_LABEL = {
  pending: '待确认',
  confirmed: '已确认',
  rejected: '已拒绝',
  cancelled: '已取消',
}

export default function MyPanel({ user, onClose, onOpenHouse }) {
  const [tab, setTab] = useState('appointments')
  const [appointments, setAppointments] = useState([])
  const [favorites, setFavorites] = useState([])
  const [error, setError] = useState('')

  async function load() {
    setError('')
    try {
      const [mine, favs] = await Promise.all([
        api.myAppointments(),
        api.listFavorites(),
      ])
      setAppointments(mine.list || [])
      setFavorites(favs.list || [])
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  async function change(id, status) {
    try {
      await api.updateAppointment(id, status)
      load()
    } catch (err) {
      setError(err.message)
    }
  }

  const isStaff = user.role === 'landlord' || user.role === 'admin'

  return (
    <>
      <div className="drawer-mask" onClick={onClose} />
      <aside className="drawer">
        <button className="drawer-close" onClick={onClose} aria-label="关闭">
          ×
        </button>
        <h3>{user.real_name || user.username}</h3>
        <div className="loc">
          {{ tenant: '租客', landlord: '房东', admin: '管理员' }[user.role]} ·{' '}
          {user.phone}
        </div>

        <div className="tabs">
          <button
            className={`tab ${tab === 'appointments' ? 'active' : ''}`}
            onClick={() => setTab('appointments')}
          >
            {isStaff ? '收到的预约' : '我的预约'}（{appointments.length}）
          </button>
          <button
            className={`tab ${tab === 'favorites' ? 'active' : ''}`}
            onClick={() => setTab('favorites')}
          >
            我的收藏（{favorites.length}）
          </button>
        </div>

        {error && <div className="reply failed">{error}</div>}

        {tab === 'appointments' &&
          (appointments.length === 0 ? (
            <p className="desc">还没有预约记录。</p>
          ) : (
            <ul className="record-list">
              {appointments.map((item) => (
                <li key={item.id}>
                  <div className="record-head">
                    <strong>{item.house_no}</strong>
                    <span className={`pill status-${item.status}`}>
                      {STATUS_LABEL[item.status] || item.status}
                    </span>
                  </div>
                  <div className="record-body">
                    {item.viewing_time.slice(0, 16)} · {item.contact_name}
                  </div>
                  {item.reply && <div className="record-body">房东回复：{item.reply}</div>}

                  {item.status === 'pending' && (
                    <div className="record-actions">
                      {isStaff && (
                        <>
                          <button
                            className="btn small"
                            onClick={() => change(item.id, 'confirmed')}
                          >
                            确认
                          </button>
                          <button
                            className="btn ghost small"
                            onClick={() => change(item.id, 'rejected')}
                          >
                            拒绝
                          </button>
                        </>
                      )}
                      <button
                        className="btn ghost small"
                        onClick={() => change(item.id, 'cancelled')}
                      >
                        取消
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          ))}

        {tab === 'favorites' &&
          (favorites.length === 0 ? (
            <p className="desc">还没有收藏房源。</p>
          ) : (
            <ul className="record-list">
              {favorites.map((house) => (
                <li key={house.house_no}>
                  <div className="record-head">
                    <strong>{house.title}</strong>
                    <span className="pill">{Math.round(house.rent_price)} 元</span>
                  </div>
                  <div className="record-body">
                    {house.district} · {house.layout} · {Math.round(house.area)} 平米
                  </div>
                  <div className="record-actions">
                    <button className="btn small" onClick={() => onOpenHouse(house)}>
                      查看
                    </button>
                    <button
                      className="btn ghost small"
                      onClick={async () => {
                        await api.removeFavorite(house.house_no)
                        load()
                      }}
                    >
                      取消收藏
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          ))}
      </aside>
    </>
  )
}
