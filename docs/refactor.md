
## Project Model

Project model:

```
                User
                  │
                  ▼
              REPL Loop
        (input/output lifecycle)
                  │
                  ▼
              Session
        (conversation state owner)
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
   Agent Loop          Context Manager
 (reasoning/action)   (state + retrieval)
        │                   │
        └─────────┬─────────┘
                  ▼
              Tools
```

* REPL loop → talks to session
* Agent loop → consumes context
* Context manager → persists and prepares state
* Session → coordinates lifecycle

Dependency direction:

```
                 CLI
                  |
                  v
               Session
                  |
       +----------+----------+
       |                     |
       v                     v
    Agent              ContextManager
       |                     |
       v                     v
    Tools                  EventStore
       |
       v
      LLM
```

## Project structure

Recommended project structure:

```
├── coding/
│   └── toddler/
│       │
│       ├── main.py                  # CLI entry point
│       │
│       ├── cli/                     # REPL layer
│       │   ├── repl.py              # interactive loop
│       │   ├── commands.py          # /reset, /exit, /diff
│       │   └── renderer.py           # terminal output
│       │
│       ├── session/                 # lifecycle owner
│       │   ├── session.py            # orchestrates everything
│       │   ├── events.py             # event definitions
│       │   └── store.py              # event persistence
│       │
│       ├── agent/                   # agent loop
│       │   ├── loop.py               # think -> act -> observe
│       │   ├── planner.py
│       │   └── state.py
│       │
│       ├── context/                 # context management
│       │   ├── manager.py            # ContextManager
│       │   ├── builder.py            # build LLM prompt context
│       │   ├── conversation.py       # messages
│       │   ├── workspace.py          # repo state
│       │   └── summarizer.py
│       │
│       ├── tools/                   # agent capabilities
│       │   ├── registry.py
│       │   ├── filesystem.py
│       │   ├── shell.py
│       │   ├── git.py
│       │   └── search.py
│       │
│       ├── llm/                     # model integration
│       │   ├── provider.py
│       │   ├── messages.py
│       │   └── responses.py
│       │
│       ├── workspace/               # codebase understanding
│       │   ├── scanner.py
│       │   ├── files.py
│       │   └── symbols.py
│       │
│       ├── config/
│       │   ├── settings.py
│       │   └── defaults.py
│       │
│       └── utils/
│           ├── logging.py
│           └── paths.py
```

**Note**: files in each directory are just hints of functionality or abstraction of that package. They should not be treated as is, but as an example.
