import type { Item, Occurrence } from '../api/types.ts'
import type { FeedGroups } from '../app/feedGroups.ts'
import { dayLabel, isEmpty } from '../app/feedGroups.ts'
import { rowId, type Row } from '../app/rows.ts'
import styles from '../App.module.css'
import { EmptyState } from './EmptyState.tsx'
import { formatTime, occurrenceState } from './format.ts'
import { NeedsLookCard } from './NeedsLookCard.tsx'
import { ReminderCard } from './ReminderCard.tsx'

export interface FeedListProps {
  groups: FeedGroups
  timeZone: string
  busy: ReadonlySet<number>
  weekOpen: boolean
  onToggleWeek: () => void
  onDone: (item: Item) => void
  onSnooze: (item: Item) => void
  onAnswer: (item: Item, phrase: string) => Promise<void>
  onDismiss: (item: Item) => void
}

/**
 * The one list (#22): Needs a look, Next up, Today, Tomorrow, This week (collapsed to a count),
 * Anytime. Each group appears only when it has something in it.
 */
export function FeedList({
  groups,
  timeZone,
  busy,
  weekOpen,
  onToggleWeek,
  onDone,
  onSnooze,
  onAnswer,
  onDismiss,
}: FeedListProps) {
  const card = (occurrence: Occurrence, options: { emphasis?: boolean; day?: boolean } = {}) => {
    const row: Row = { kind: 'occurrence', occurrence }
    const time = formatTime(occurrence.at, timeZone)
    return (
      <ReminderCard
        key={rowId(row)}
        id={rowId(row)}
        item={occurrence.item}
        timeZone={timeZone}
        timeLabel={options.day ? `${dayLabel(occurrence.at, timeZone)} ${time}` : time}
        stateLabel={occurrenceState(occurrence, timeZone)}
        actions={occurrence.acts}
        emphasis={options.emphasis}
        busy={busy.has(occurrence.item.id)}
        onDone={onDone}
        onSnooze={onSnooze}
      />
    )
  }

  const section = (name: string, children: React.ReactNode, heading?: React.ReactNode) => (
    <section className={styles.group} aria-labelledby={`group-${name.replace(/\s+/g, '-')}`}>
      <h2 className={styles.groupName} id={`group-${name.replace(/\s+/g, '-')}`}>
        {heading ?? name}
      </h2>
      {children}
    </section>
  )

  const nothingTimed = groups.nextUp === null && groups.anytime.length === 0 && groups.later === 0

  return (
    <>
      {groups.needsLook.length > 0 &&
        section(
          'Needs a look',
          groups.needsLook.map((item) => (
            <NeedsLookCard
              key={rowId({ kind: 'needs', item })}
              id={rowId({ kind: 'needs', item })}
              item={item}
              onAnswer={onAnswer}
              onDismiss={onDismiss}
              busy={busy.has(item.id)}
            />
          )),
        )}

      {isEmpty(groups) && (
        <EmptyState
          title="Nothing coming up"
          hint="Try “text me tomorrow at 9am to call the bank” below."
        />
      )}
      {!isEmpty(groups) && nothingTimed && (
        <EmptyState
          title="Nothing scheduled"
          hint="Answer what needs a look, or add a reminder below."
        />
      )}

      {groups.nextUp && section('Next up', card(groups.nextUp, { emphasis: true }))}
      {groups.today.length > 0 &&
        section(
          'Today',
          groups.today.map((o) => card(o)),
        )}
      {groups.tomorrow.length > 0 &&
        section(
          'Tomorrow',
          groups.tomorrow.map((o) => card(o)),
        )}
      {groups.thisWeek.length > 0 &&
        section(
          'This week',
          weekOpen ? groups.thisWeek.map((o) => card(o, { day: true })) : null,
          <button
            type="button"
            className={styles.groupToggle}
            onClick={onToggleWeek}
            aria-expanded={weekOpen}
          >
            This week{' '}
            <span className={styles.count}>
              {weekOpen ? 'hide' : `${String(groups.thisWeek.length)} more ›`}
            </span>
          </button>,
        )}
      {groups.anytime.length > 0 &&
        section(
          'Anytime',
          groups.anytime.map((item) => {
            const row: Row = { kind: 'task', item }
            return (
              <ReminderCard
                key={rowId(row)}
                id={rowId(row)}
                item={item}
                timeZone={timeZone}
                timeLabel="—"
                stateLabel={null}
                snoozeLabel="In 1 hour"
                busy={busy.has(item.id)}
                onDone={onDone}
                onSnooze={onSnooze}
              />
            )
          }),
        )}
      {groups.later > 0 && (
        <p className={styles.later}>
          {groups.later === 1 ? '1 more reminder' : `${String(groups.later)} more reminders`} after
          this week
        </p>
      )}
    </>
  )
}
