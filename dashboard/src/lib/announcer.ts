/**
 * The live-region context. The provider component lives in
 * src/components/Announcer.tsx; the context and hook live here so a module
 * that only needs to announce does not import a component.
 */
import { createContext, useContext } from 'react'

export interface AnnouncerValue {
  message: string
  announce: (message: string) => void
}

export const AnnouncerContext = createContext<AnnouncerValue>({
  message: '',
  announce: () => {},
})

export function useAnnounce(): (message: string) => void {
  return useContext(AnnouncerContext).announce
}
