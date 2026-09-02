from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import is_html_empty


class CyanMeetingResolution(models.Model):
    _name = "cyan.meeting.resolution"
    _description = "Meeting Resolution"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "deadline asc, priority desc, sequence, id"
    _check_company_auto = True

    _ESCALATION_DAYS_BY_PRIORITY = {
        "0": 3,
        "1": 2,
        "2": 1,
        "3": 1,
    }

    sequence = fields.Integer(default=10)
    meeting_id = fields.Many2one(
        "cyan.meeting.minute", required=True, ondelete="cascade", index=True,
        check_company=True, tracking=True,
    )
    company_id = fields.Many2one(
        "res.company", related="meeting_id.company_id", store=True, readonly=True, index=True,
    )
    name = fields.Char(string="Resolution", required=True, tracking=True)
    description = fields.Html(sanitize=True)
    responsible_id = fields.Many2one(
        "res.users", required=True, domain="[('share', '=', False)]",
        check_company=True, tracking=True, index=True,
    )
    deadline = fields.Date(required=True, tracking=True, index=True)
    priority = fields.Selection(
        [("0", "Normal"), ("1", "Important"), ("2", "Very Important"), ("3", "Urgent")],
        default="0", required=True, index=True, tracking=True,
    )
    state = fields.Selection(
        [("open", "Open"), ("in_progress", "In Progress"), ("done", "Done"), ("cancelled", "Cancelled")],
        default="open", required=True, index=True, tracking=True,
    )
    completion_date = fields.Date(readonly=True, tracking=True)
    result = fields.Html(string="Completion Notes", sanitize=True)
    activity_id = fields.Many2one("mail.activity", readonly=True, copy=False, ondelete="set null")
    days_overdue = fields.Integer(compute="_compute_deadline_metrics", string="Days Late")
    is_due_soon = fields.Boolean(compute="_compute_deadline_metrics", search="_search_is_due_soon")
    is_escalated = fields.Boolean(readonly=True, copy=False, index=True, tracking=True)
    escalated_on = fields.Datetime(readonly=True, copy=False, tracking=True)
    escalated_to_id = fields.Many2one(
        "res.users", string="Escalated To", readonly=True, copy=False,
        check_company=True, index=True, tracking=True,
    )
    deadline_status = fields.Selection(
        [("overdue", "Overdue"), ("today", "Due Today"), ("upcoming", "Upcoming"), ("completed", "Completed")],
        compute="_compute_deadline_status", search="_search_deadline_status",
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_activity()
        for record in records:
            record.meeting_id.message_post(
                body=Markup("Resolution created: <strong>%s</strong>") % record.name,
                subtype_xmlid="mail.mt_note",
            )
        return records

    def write(self, vals):
        escalation_fields = {"is_escalated", "escalated_on", "escalated_to_id"}
        if escalation_fields.intersection(vals) and not self.env.user.has_group(
            "cyan_meeting_minutes.group_meeting_manager"
        ):
            raise UserError(_("Only a Meeting Manager may update escalation details."))
        if any(record.meeting_id.state == "done" for record in self):
            if not self.env.user.has_group("cyan_meeting_minutes.group_meeting_manager"):
                raise UserError(_("Only a Meeting Manager may edit resolutions of a completed meeting."))
        if "state" in vals:
            if vals["state"] == "done":
                vals.setdefault("completion_date", fields.Date.context_today(self))
            elif vals["state"] != "done":
                vals.setdefault("completion_date", False)
        result = super().write(vals)
        if {"responsible_id", "deadline", "name", "meeting_id", "state"}.intersection(vals):
            self._sync_activity()
        return result

    def unlink(self):
        self.mapped("activity_id").exists().unlink()
        return super().unlink()

    @api.constrains("responsible_id", "company_id")
    def _check_responsible_company(self):
        for resolution in self:
            if resolution.company_id not in resolution.responsible_id.company_ids:
                raise ValidationError(_("The responsible user must have access to the meeting company."))

    @api.constrains("state", "result", "is_escalated")
    def _check_escalated_completion_notes(self):
        for resolution in self:
            if resolution.is_escalated and resolution.state == "done" and is_html_empty(resolution.result):
                raise ValidationError(_("Completion Notes are required to complete an escalated resolution."))

    @api.depends("deadline", "state")
    def _compute_deadline_metrics(self):
        today = fields.Date.context_today(self)
        due_soon_limit = today + timedelta(days=2)
        for resolution in self:
            is_active = resolution.state not in ("done", "cancelled")
            resolution.days_overdue = (
                (today - resolution.deadline).days
                if is_active and resolution.deadline and resolution.deadline < today
                else 0
            )
            resolution.is_due_soon = bool(
                is_active and resolution.deadline
                and today < resolution.deadline <= due_soon_limit
            )

    @api.model
    def _search_is_due_soon(self, operator, value):
        if operator not in ("=", "!="):
            raise UserError(_("Due Soon only supports equality searches."))
        today = fields.Date.context_today(self)
        due_soon_limit = today + timedelta(days=2)
        domain = [
            ("deadline", ">", today),
            ("deadline", "<=", due_soon_limit),
            ("state", "not in", ("done", "cancelled")),
        ]
        matches = bool(value) if operator == "=" else not bool(value)
        return domain if matches else ["!"] + domain

    @api.depends("deadline", "state")
    def _compute_deadline_status(self):
        today = fields.Date.context_today(self)
        for resolution in self:
            if resolution.state in ("done", "cancelled"):
                resolution.deadline_status = "completed"
            elif resolution.deadline and resolution.deadline < today:
                resolution.deadline_status = "overdue"
            elif resolution.deadline == today:
                resolution.deadline_status = "today"
            else:
                resolution.deadline_status = "upcoming"

    @api.model
    def _search_deadline_status(self, operator, value):
        if operator not in ("=", "!="):
            raise UserError(_("Deadline status only supports equality searches."))
        today = fields.Date.context_today(self)
        domains = {
            "overdue": [("deadline", "<", today), ("state", "not in", ("done", "cancelled"))],
            "today": [("deadline", "=", today), ("state", "not in", ("done", "cancelled"))],
            "upcoming": [("deadline", ">", today), ("state", "not in", ("done", "cancelled"))],
            "completed": [("state", "in", ("done", "cancelled"))],
        }
        domain = domains.get(value, [("id", "=", 0)])
        return domain if operator == "=" else ["!"] + domain

    @api.model
    def _cron_check_overdue_escalations(self, additional_domain=None):
        today = fields.Date.context_today(self)
        domain = [
            ("state", "not in", ("done", "cancelled")),
            ("deadline", "<", today),
            ("is_escalated", "=", False),
        ]
        if additional_domain:
            domain += additional_domain
        candidates = self.search(domain)
        for resolution in candidates:
            days_overdue = (today - resolution.deadline).days
            threshold = self._ESCALATION_DAYS_BY_PRIORITY[resolution.priority]
            if days_overdue < threshold:
                continue
            organizer = resolution.meeting_id.organizer_id
            resolution.write({
                "is_escalated": True,
                "escalated_on": fields.Datetime.now(),
                "escalated_to_id": organizer.id,
            })
            resolution.message_post(
                body=Markup(
                    "<p><strong>%s</strong></p>"
                    "<p>%s: %s<br/>%s: %s<br/>%s: %s<br/>%s: %s</p>"
                ) % (
                    _("Resolution escalated"),
                    _("Days overdue"), days_overdue,
                    _("Responsible"), resolution.responsible_id.display_name,
                    _("Deadline"), resolution.deadline,
                    _("Escalated to"), organizer.display_name,
                ),
                subtype_xmlid="mail.mt_note",
            )
        return True

    def _activity_values(self):
        self.ensure_one()
        return {
            "user_id": self.responsible_id.id,
            "date_deadline": self.deadline,
            "summary": _("Meeting %(reference)s — %(resolution)s", reference=self.meeting_id.reference, resolution=self.name),
            "note": Markup("<p><strong>%s</strong></p><p>%s</p>") % (self.meeting_id.name, self.description or ""),
        }

    def _sync_activity(self):
        todo_type = self.env.ref("mail.mail_activity_data_todo")
        for resolution in self:
            activity = resolution.activity_id.exists()
            should_exist = bool(
                resolution.responsible_id and resolution.deadline
                and resolution.state not in ("done", "cancelled")
            )
            if should_exist:
                values = resolution._activity_values()
                if activity:
                    activity.write(values)
                else:
                    activity = resolution.activity_schedule(
                        "mail.mail_activity_data_todo",
                        date_deadline=resolution.deadline,
                        summary=values["summary"],
                        note=values["note"],
                        user_id=resolution.responsible_id.id,
                    )
                    resolution.activity_id = activity.id
            elif activity:
                if resolution.state == "done":
                    activity.action_feedback(feedback=_("Resolution completed."))
                else:
                    activity.unlink()
                resolution.activity_id = False
        return True

    def action_start(self):
        self.filtered(lambda item: item.state == "open").write({"state": "in_progress"})
        return True

    def action_done(self):
        self.filtered(lambda item: item.state not in ("done", "cancelled")).write({"state": "done"})
        return True

    def action_cancel(self):
        self.filtered(lambda item: item.state != "cancelled").write({"state": "cancelled"})
        return True

    def action_reopen(self):
        self.write({"state": "open"})
        return True
