// Static sample data from docs/design/mobile/Today.dc.html, for the scaffold's placeholder page.
// The in-memory MockClient behind the real client interface replaces this in #101.

export type SampleGroup = 'Today' | 'Tomorrow' | 'Later'

export interface SampleReminder {
  id: number
  group: SampleGroup
  time: string
  text: string
  done: boolean
}

export const sampleReminders: readonly SampleReminder[] = [
  { id: 1, group: 'Today', time: '8:30', text: 'Put the bins out', done: true },
  { id: 2, group: 'Today', time: '12:00', text: 'Call the bank about the card', done: false },
  { id: 3, group: 'Today', time: '17:45', text: 'Pick up the kids from practice', done: false },
  { id: 4, group: 'Today', time: '21:00', text: 'Water the plants', done: false },
  { id: 5, group: 'Tomorrow', time: '15:00', text: 'Dentist', done: false },
  { id: 6, group: 'Later', time: 'Fri', text: 'Send the school trip form', done: false },
]
