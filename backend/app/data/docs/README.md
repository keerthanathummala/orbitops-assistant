The base OrbitOps corpus: runbooks and process docs (OO-001 to OO-012) that
the RAG pipeline indexes at startup and the AIOps health check cites.

Each `.txt` file has a metadata header, a blank line, then the body
(`Doc ID:`, `Title:`, `Date:`, `Team:`, `Tags:`). Add more files in the same
format and rebuild the backend image to include them.
