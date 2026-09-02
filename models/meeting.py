from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class CyanMeetingMinute(models.Model):
    _name = "cyan.meeting.minute"
    _description = "Internal Meeting Minutes"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "reference"
    _order = "meeting_date desc, id desc"
    _check_company_auto = True

    reference = fields.Char(
        string="Reference", required=True, readonly=True, copy=False,
        default=lambda self: _("New"), index="btree", tracking=True,
    )
    name = fields.Char(string="Meeting Title", required=True, tracking=True)
    company_id = fields.Many2one(
        "res.company", required=True, index=True, default=lambda self: self.env.company,
        tracking=True,
    )
    meeting_date = fields.Date(required=True, default=fields.Date.context_today, tracking=True, index=True)
    start_datetime = fields.Datetime(string="Start", tracking=True)
    end_datetime = fields.Datetime(string="End", tracking=True)
    location = fields.Char(tracking=True)
    organizer_id = fields.Many2one(
        "res.users", string="Organizer", required=True, default=lambda self: self.env.user,
        domain="[('share', '=', False)]", check_company=True, tracking=True,
    )
    attendee_ids = fields.Many2many(
        "res.users", "cyan_meeting_attendee_rel", "meeting_id", "user_id",
        string="Attendees", domain="[('share', '=', False)]", check_company=True,
        tracking=True,
    )
    agenda = fields.Html(sanitize=True)
    minutes = fields.Html(string="Meeting Minutes", sanitize=True)
    resolution_ids = fields.One2many("cyan.meeting.resolution", "meeting_id", string="Resolutions")
    state = fields.Selection(
        [("draft", "Draft"), ("confirmed", "Confirmed"), ("done", "Completed"), ("cancelled", "Cancelled")],
        default="draft", required=True, index=True, tracking=True,
    )
    active = fields.Boolean(default=True)
    resolution_count = fields.Integer(compute="_compute_resolution_counts", string="Total Resolutions")
    open_resolution_count = fields.Integer(compute="_compute_resolution_counts", string="Open Resolutions")
    overdue_resolution_count = fields.Integer(compute="_compute_resolution_counts", string="Overdue Resolutions")

    _reference_unique = models.Constraint("unique(reference)", "Meeting reference must be unique.")

    @api.model_create_multi
    def create(self, vals_list):
        sequence = self.env["ir.sequence"]
        for vals in vals_list:
            if vals.get("reference", _("New")) == _("New"):
                vals["reference"] = sequence.next_by_code("cyan.meeting.minute") or _("New")
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("state") in ("done", "cancelled"):
            if not self.env.user.has_group("cyan_meeting_minutes.group_meeting_manager"):
                raise UserError(_("Only a Meeting Manager may complete or cancel meetings."))
        if vals.get("state") == "draft" and any(record.state in ("done", "cancelled") for record in self):
            if not self.env.user.has_group("cyan_meeting_minutes.group_meeting_manager"):
                raise UserError(_("Only a Meeting Manager may reopen completed or cancelled meetings."))
        protected = {
            "name", "company_id", "meeting_date", "start_datetime", "end_datetime",
            "location", "organizer_id", "attendee_ids", "agenda", "minutes", "resolution_ids",
        }
        if protected.intersection(vals) and any(record.state == "done" for record in self):
            if not self.env.user.has_group("cyan_meeting_minutes.group_meeting_manager"):
                raise UserError(_("Only a Meeting Manager may edit completed meeting information."))
        return super().write(vals)

    @api.constrains("start_datetime", "end_datetime")
    def _check_datetime_order(self):
        for meeting in self:
            if meeting.start_datetime and meeting.end_datetime and meeting.end_datetime < meeting.start_datetime:
                raise ValidationError(_("The meeting end time cannot be earlier than its start time."))

    @api.constrains("organizer_id", "attendee_ids", "company_id")
    def _check_user_companies(self):
        for meeting in self:
            users = meeting.organizer_id | meeting.attendee_ids
            invalid = users.filtered(lambda user: meeting.company_id not in user.company_ids)
            if invalid:
                raise ValidationError(_("Organizer and attendees must have access to the meeting company."))

    @api.depends("resolution_ids.state", "resolution_ids.deadline")
    def _compute_resolution_counts(self):
        for meeting in self:
            meeting.resolution_count = 0
            meeting.open_resolution_count = 0
            meeting.overdue_resolution_count = 0

        persisted_meetings = self.filtered(lambda meeting: meeting.id)
        if not persisted_meetings:
            return

        values = {meeting.id: [0, 0, 0] for meeting in persisted_meetings}
        meeting_domain = [("meeting_id", "in", persisted_meetings.ids)]
        today = fields.Date.context_today(self)
        Resolution = self.env["cyan.meeting.resolution"]
        for meeting, count in Resolution._read_group(
            meeting_domain, ["meeting_id"], ["__count"],
        ):
            values[meeting.id][0] = count
        for meeting, count in Resolution._read_group(
            meeting_domain + [("state", "not in", ("done", "cancelled"))],
            ["meeting_id"], ["__count"],
        ):
            values[meeting.id][1] = count
        for meeting, count in Resolution._read_group(
            meeting_domain + [
                ("state", "not in", ("done", "cancelled")),
                ("deadline", "<", today),
            ],
            ["meeting_id"], ["__count"],
        ):
            values[meeting.id][2] = count
        for meeting in persisted_meetings:
            meeting.resolution_count, meeting.open_resolution_count, meeting.overdue_resolution_count = values[meeting.id]

    def action_confirm(self):
        for meeting in self:
            if meeting.state != "draft":
                raise UserError(_("Only draft meetings can be confirmed."))
        self.write({"state": "confirmed"})
        return True

    def action_done(self):
        for meeting in self:
            if meeting.state != "confirmed":
                raise UserError(_("Only confirmed meetings can be completed."))
        self.write({"state": "done"})
        return True

    def action_cancel(self):
        self.filtered(lambda meeting: meeting.state != "cancelled").write({"state": "cancelled"})
        return True

    def action_reset_draft(self):
        self.write({"state": "draft"})
        return True

    def action_view_resolutions(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("cyan_meeting_minutes.action_meeting_resolution")
        action["domain"] = [("meeting_id", "=", self.id)]
        action["context"] = {"default_meeting_id": self.id}
        return action
