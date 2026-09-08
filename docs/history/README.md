# History

**Nothing in this directory describes the harness as it is now.** These are
closed records, kept because the reasoning in them is worth more than the
conclusions — what was found, what was decided, what was decided *against*, and
which of those calls turned out to be wrong.

| | |
| --- | --- |
| [`self-review.md`](self-review.md) | The harness reviewed its own source and produced 88 findings. This is how the 37 that were still open got closed: the batches, what counted as done for each, and the calls made along the way. It is also where the control-plane framing was first written down. |
| [`development-plan.md`](development-plan.md) | The four items of work that followed, in the order they were done and with the argument for that order. Two of its own recommendations were overruled by the user, and both the recommendation and the override are still in it. |
| [`quality-assessment.md`](quality-assessment.md) | The assessment that set the standard: ten criteria written before anything was measured, the instruments that measured against them, and seventeen findings — all closed. Its criteria are live and moved to [`../quality-standard.md`](../quality-standard.md); what stays here is what was found. |
| [`shared-context-spec.md`](shared-context-spec.md) | The design pass for shared semantic context, written before the code the way the envelope's was. Kept for §5, the three choices that were genuinely undetermined, and §8, which records how they were decided and the one place the implementation departed from the spec. |

They are written in the present tense of the day they were written, and they are
left that way. A record edited to match what happened afterwards is no longer
evidence of what was believed at the time, and what was believed at the time is
the only thing these can tell you that the code cannot.

For what is true now:

| | |
| --- | --- |
| [`../reasoning-control-plane.md`](../reasoning-control-plane.md) | What the harness is |
| [`../architecture.md`](../architecture.md) | How a run works, drawn |
| [`../quality-standard.md`](../quality-standard.md) | The standard it is held to, and the gates that enforce it |
| [`../development-plan.md`](../development-plan.md) | The work that is scheduled |
