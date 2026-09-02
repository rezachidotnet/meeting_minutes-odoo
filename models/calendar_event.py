from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class CalendarEvent(models.Model):
    _inherit = "calendar.event"

    meeting_minute_ids = fields.One2many(
        "cyan.meeting.minute", "calendar_event_id", string="Meeting Minutes",
    )
    meeting_minute_count = fields.Integer(compute="_compute_meeting_minute_count")

    @api.depends("meeting_minute_ids")
    def _compute_meeting_minute_count(self):
        for event in self:
            event.meeting_minute_count = len(event.meeting_minute_ids)

    def _cyan_internal_attendees(self, company, organizer):
        self.ensure_one()
        users = self.partner_ids.mapped("user_ids").filtered(
            lambda user: user.active and not user.share and company in user.company_ids
        )
        return users - organizer

    def _cyan_meeting_schedule_values(self, company=None, fallback_organizer=None):
        self.ensure_one()
        company = company or (
            self.user_id.company_id
            if self.user_id and self.user_id.company_id in self.env.companies
            else self.env.company
        )
        organizer = self.user_id
        if not organizer or company not in organizer.company_ids:
            organizer = fallback_organizer or self.env.user
        if company not in organizer.company_ids:
            raise UserError(_("The Calendar organizer must have access to the Meeting Minutes company."))
        start_in_organizer_tz = fields.Datetime.context_timestamp(
            self.with_context(tz=organizer.tz), self.start,
        )
        return {
            "name": self.name,
            "company_id": company.id,
            "meeting_date": self.start_date if self.allday else start_in_organizer_tz.date(),
            "start_datetime": self.start,
            "end_datetime": self.stop,
            "location": self.location,
            "organizer_id": organizer.id,
            "attendee_ids": [(6, 0, self._cyan_internal_attendees(company, organizer).ids)],
        }

    def action_open_meeting_minutes(self):
        self.ensure_one()
        meeting = self.meeting_minute_ids[:1]
        if not meeting:
            values = self._cyan_meeting_schedule_values()
            values["calendar_event_id"] = self.id
            try:
                with self.env.cr.savepoint():
                    meeting = self.env["cyan.meeting.minute"].create(values)
            except IntegrityError:
                meeting = self.env["cyan.meeting.minute"].search(
                    [("calendar_event_id", "=", self.id)], limit=1,
                )
                if not meeting:
                    raise
            meeting.message_post(
                body=_("Linked to Calendar event: %s", self.name),
                subtype_xmlid="mail.mt_note",
            )
        meeting.check_access("read")
        return {
            "type": "ir.actions.act_window",
            "name": _("Meeting Minutes"),
            "res_model": "cyan.meeting.minute",
            "res_id": meeting.id,
            "view_mode": "form",
            "target": "current",
        }

    def write(self, vals):
        result = super().write(vals)
        schedule_fields = {"name", "start", "stop", "allday", "location", "user_id", "partner_ids"}
        if schedule_fields.intersection(vals):
            for event in self:
                meeting = event.meeting_minute_ids[:1]
                if meeting:
                    schedule_values = event._cyan_meeting_schedule_values(
                        company=meeting.company_id,
                        fallback_organizer=meeting.organizer_id,
                    )
                    meeting.sudo().with_context(
                        cyan_calendar_sync=True,
                        tracking_disable=True,
                    ).write(schedule_values)
        return result
