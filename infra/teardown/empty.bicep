// Complete-mode teardown of rg-docchat-load (83-infrastructure.md section 10.14; ADR 0126): a
// template with no resources, so a complete-mode deployment removes every resource of the run
// while the group's role and policy assignments, which complete mode never deletes, survive.

targetScope = 'resourceGroup'
