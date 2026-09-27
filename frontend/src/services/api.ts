import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api/v1',
})

// Автоматическая подстановка JWT
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

// Эндпоинты аутентификации: 401 здесь — это «неверные креды», а не «токен протух».
// Refresh-retry на них запрещён: /auth/login/ сам возвращает 401, иначе каждая ошибка
// входа уходила бы в попытку рефреша, а её 400 — в жёсткую перезагрузку /login.
const AUTH_URLS = ['/auth/login/', '/auth/register/', '/auth/refresh/']

function isAuthRequest(url?: string): boolean {
  return !!url && AUTH_URLS.some((path) => url.includes(path))
}

// Авто-refresh при 401
api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const original = error.config
    if (error.response?.status === 401 && !original._retry && !isAuthRequest(original?.url)) {
      original._retry = true
      try {
        const refresh = localStorage.getItem('refresh_token')
        const { data } = await axios.post(
          `${api.defaults.baseURL}/auth/refresh/`,
          { refresh },
        )
        localStorage.setItem('access_token', data.access)
        original.headers.Authorization = `Bearer ${data.access}`
        return api(original)
      } catch {
        localStorage.removeItem('access_token')
        localStorage.removeItem('refresh_token')
        // Перезагрузка сбрасывает состояние store — на /login она стёрла бы текст
        // ошибки входа, который как раз и нужно показать.
        if (window.location.pathname !== '/login') window.location.href = '/login'
      }
    }
    return Promise.reject(error)
  },
)

export const fetchMe = () => api.get('/users/me/')

export default api
