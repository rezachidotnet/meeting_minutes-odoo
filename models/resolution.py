from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class CyanMeetingResolution(models.Model):
    _name = "cyan.meeting.resolution"
    _description = "Meeting Resolution"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "deadline asc, priority desc, sequence, id"
    _check_company_auto = True

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

