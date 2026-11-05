import { sampleReminders, type SampleGroup } from './mock/sample.ts'

const GROUPS: readonly SampleGroup[] = ['Today', 'Tomorrow', 'Later']

// Placeholder for the scaffold (#100): proves the app builds and renders mock data with no backend.
export default function App() {
  const left = sampleReminders.filter((r) => r.group === 'Today' && !r.done).length

  return (
    <div className="app">
      <header className="header">
        <h1>Maestro</h1>
        <p className="summary">{left} left today</p>
        <p className="badge">Mock data · no server</p>
      </header>

      <main className="content">
        {GROUPS.map((group) => {
          const items = sampleReminders.filter((r) => r.group === group)
          if (items.length === 0) return null
          return (
            <section key={group} aria-labelledby={`group-${group}`}>
              <h2 id={`group-${group}`}>{group}</h2>
              <ul>
                {items.map((r) => (
                  <li key={r.id} className={r.done ? 'done' : undefined}>
                    <span className="time">{r.time}</span>
                    <span>
                      {r.text}
                      {r.done && <span className="visually-hidden"> (done)</span>}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )
        })}
        <p className="note">Placeholder page. The real screens arrive with #103–#107.</p>
      </main>
    </div>
  )
}
