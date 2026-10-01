// The Resume button under a turn that died. Both chat views render it.
export default function ResumeBar({ onResume, compact = false }) {
  return (
    <div className={`resume-bar${compact ? ' compact' : ''}`}>
      <button type="button" className="ghost" onClick={onResume}
              title="Send “Continue from where the previous turn stopped.” The model gets back the steps this turn had already run.">
        Resume
      </button>
      <span className="dim">continues from the last step it finished</span>
    </div>
  )
}
