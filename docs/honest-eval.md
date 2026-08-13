# Honest evaluation: instrument the lever, or the null is noise

This toolkit ships with experiment instrumentation (`assist_report`, the
fired-signal fields) because of a methodological near-miss in our own
evaluation, and the rule it taught deserves to travel with the code.

## The rule

Before an expensive measurement run of any augmentation lever, add two things:

1. **A fired-signal**: a per-unit record that the treatment actually executed —
   a call count, an injected flag, a grounded-symbol count. If one arm of the
   experiment has it, EVERY arm needs the symmetric version. Asymmetric
   instrumentation is the smell.
2. **An input preflight**: a check that the data the treatment consumes is
   actually present (for a graph lever: a per-predicate fact census), with a
   loud warning when it is not. A lever starved of input reads exactly like an
   ineffective lever.

Without both, a null result is a wasted run: you cannot publish "it does not
help", because you cannot rule out "it never ran".

## The near-miss that taught it

We evaluated this graph as a serve-time recall layer for a small (7B-class)
local coding model on a suite of symptom-phrased diagnosis tasks — the model
gets a bug report, must find the site, and land the fix. Two experiment arms
augmented the model with the graph (pre-injection and agentic tools), anchored
by a bare arm (floor) and an oracle arm fed perfect grounding (ceiling).

The pre-injection arm initially recorded *nothing* about whether injection
happened. A whole-system review caught it before the run: if the graph had
been missing its enrichment relations, the arm would have silently degraded
to a no-op on every task, and the resulting 0 would have been unreadable —
"retrieval does not help" and "the lever never fired" produce the same
number. The instrumentation landed first; only then did the run.

## What the instrumented run showed

- The levers **fired**: injection grounded 5–6 symbols on every task
  (verified per-task), with correct file:line on the target every time the
  suite's oracle agreed.
- The model's scores did **not** move: augmented arms equaled the bare floor.
- The **oracle ceiling was the tell**: even handed the exact file, symbol,
  and line, the model still failed most tasks on the *value* of the edit —
  wrong constant, wrong range boundary, wrong parameter.

Conclusion: for that model class, the binding constraint was **edit
generation, not retrieval**. Recall infrastructure grounds the task; it
cannot write the fix. We publish the method and the null rather than a
cherry-picked win, because the null is the actionable result: it tells you
where the next unit of effort goes (the model), and it certifies that when a
stronger model lands, the recall layer is already measured, instrumented, and
ready.

## Using the instrumentation

```python
from groundgraph.assist import graph_assist, assist_report

assisted = graph_assist(task, "g.db")
report = assist_report(task, assisted)
# {"injected": True, "grounded_symbols": 2}
# -> log this next to every per-task result, in every arm
```

For the input preflight, run the census before the experiment:

```sql
SELECT predicate, COUNT(*) FROM facts WHERE valid_to IS NULL GROUP BY predicate;
```

and hard-warn if the relations your lever depends on are absent.
