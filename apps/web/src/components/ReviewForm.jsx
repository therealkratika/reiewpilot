import { useState } from "react";

export default function ReviewForm({ onSubmit, busy }) {
  const [values, setValues] = useState({
    owner: "",
    repo: "",
    pullNumber: "",
    proposeFix: false,
    validateFix: false,
    targetFile: "",
  });

  function update(event) {
    setValues((current) => ({ ...current, [event.target.name]: event.target.value }));
  }

  function submit(event) {
    event.preventDefault();
    onSubmit({ ...values, pullNumber: Number(values.pullNumber) });
  }

  return (
    <form className="review-form" onSubmit={submit}>
      <label className="field">
        <span>Repository owner</span>
        <input name="owner" value={values.owner} onChange={update} placeholder="e.g. octocat" required />
      </label>
      <label className="field">
        <span>Repository name</span>
        <input name="repo" value={values.repo} onChange={update} placeholder="e.g. hello-world" required />
      </label>
      <label className="field field-number">
        <span>PR number</span>
        <input name="pullNumber" type="number" min="1" value={values.pullNumber} onChange={update} required />
      </label>
      <label className="option-field">
        <input name="proposeFix" type="checkbox" checked={values.proposeFix} onChange={(event) => setValues((current) => ({ ...current, proposeFix: event.target.checked }))} />
        <span>Generate fix proposal</span>
      </label>
      {values.proposeFix && (
        <>
          <label className="field">
            <span>Target file</span>
            <input name="targetFile" value={values.targetFile} onChange={update} placeholder="e.g. src/index.js" required />
          </label>
          <label className="option-field">
            <input name="validateFix" type="checkbox" checked={values.validateFix} onChange={(event) => setValues((current) => ({ ...current, validateFix: event.target.checked }))} />
            <span>Validate proposal in Docker</span>
          </label>
        </>
      )}
      <button className="primary-button" disabled={busy}>{busy ? "Reviewing..." : "Review Pull Request"} <span>→</span></button>
    </form>
  );
}
