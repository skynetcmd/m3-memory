# Design Philosophies

M3 Memory is built to a set of design tenets that gate every change — the
authority a contributor (human or agent) re-reads before calling work done, and
the checklist the pre-push hook echoes.

> **The full, canonical text is maintained privately** (it carries internal
> incident notes and post-mortems). This public page is the stable pointer the
> repo's docs and the pre-push hook link to, plus the one-line summary of each
> tenet. If you are working in this repo as an agent, treat the tenets below as
> binding; the numbered sections (§N) referenced throughout the docs map to this
> list.

## The tenets

1. **Local-first, sovereign, offline-capable.** 
2. **Modularity**
3. **Robustness — fail loud, fail safe** 
4. **Efficiency — don't waste resources.** 
5. **Effectiveness — does it actually work?** 
6. **Hardening, security** 
7. **Privacy & multi-tenancy.**
8. **Performance**
9. **GDPR / compliance hygiene.**
10. **Database hygiene** 
11. **Regression discipline**
12. **Clean code**

*For the full rationale, worked examples, and incident references behind each
tenet, see the private canonical document (maintainers have access).*
