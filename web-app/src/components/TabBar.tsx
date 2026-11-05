import styles from './TabBar.module.css'

export interface Tab<Id extends string> {
  id: Id
  label: string
}

export interface TabBarProps<Id extends string> {
  tabs: readonly Tab<Id>[]
  current: Id
  onSelect: (id: Id) => void
}

/**
 * The bottom navigation (Today, Lists, Notes). Buttons with aria-current, not an ARIA tablist:
 * each tab is a whole screen, so it's navigation, and every tab stays a normal Tab stop.
 */
export function TabBar<Id extends string>({ tabs, current, onSelect }: TabBarProps<Id>) {
  return (
    <nav className={styles.bar} aria-label="Sections">
      {tabs.map((t) => (
        <button
          key={t.id}
          type="button"
          className={styles.tab}
          aria-current={t.id === current ? 'page' : undefined}
          onClick={() => {
            onSelect(t.id)
          }}
        >
          {t.label}
        </button>
      ))}
    </nav>
  )
}
