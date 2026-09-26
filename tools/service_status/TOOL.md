---
name: service_status
description: Show the project's services as the host sees them - request status (pending/approved/rejected/revoked), whether each is running, its placement, exposed ports and last report time.
when_to_use: After filing a service_request, or to check whether an approved service is up.
enabled: true
read_only: true
requires_project: true
parameters:
  type: object
  properties:
    name:
      type: string
      description: Only this service (by name). Omit for all.
---
Host truth: the state comes from the host's own records and its supervisor's
last contact with the service box, not from anything the service says.
