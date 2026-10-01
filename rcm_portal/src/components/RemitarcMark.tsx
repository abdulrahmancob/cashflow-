type RemitarcMarkProps = {
  className?: string
}

export function RemitarcMark({ className = 'h-11 w-11 shrink-0' }: RemitarcMarkProps) {
  return <img src="/remitarc-mark.png" alt="" className={className} />
}
