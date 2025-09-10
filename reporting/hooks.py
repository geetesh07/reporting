app_name = "reporting"
app_title = "Reporting"
app_publisher = "NTS"
app_description = "Reporting "
app_email = "geetesh@ntechnosolution.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "reporting",
# 		"logo": "/assets/reporting/logo.png",
# 		"title": "Reporting",
# 		"route": "/reporting",
# 		"has_permission": "reporting.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/reporting/css/reporting.css"
# app_include_js = "/assets/reporting/js/reporting.js"

# include js, css files in header of web template
# web_include_css = "/assets/reporting/css/reporting.css"
# web_include_js = "/assets/reporting/js/reporting.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "reporting/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "reporting/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "reporting.utils.jinja_methods",
# 	"filters": "reporting.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "reporting.install.before_install"
# after_install = "reporting.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "reporting.uninstall.before_uninstall"
# after_uninstall = "reporting.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "reporting.utils.before_app_install"
# after_app_install = "reporting.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "reporting.utils.before_app_uninstall"
# after_app_uninstall = "reporting.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See nts.core.notifications.get_notification_config

# notification_config = "reporting.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "nts.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "nts.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"reporting.tasks.all"
# 	],
# 	"daily": [
# 		"reporting.tasks.daily"
# 	],
# 	"hourly": [
# 		"reporting.tasks.hourly"
# 	],
# 	"weekly": [
# 		"reporting.tasks.weekly"
# 	],
# 	"monthly": [
# 		"reporting.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "reporting.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"nts.desk.doctype.event.event.get_events": "reporting.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other nts apps
# override_doctype_dashboards = {
# 	"Task": "reporting.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["reporting.utils.before_request"]
# after_request = ["reporting.utils.after_request"]

# Job Events
# ----------
# before_job = ["reporting.utils.before_job"]
# after_job = ["reporting.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"reporting.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

