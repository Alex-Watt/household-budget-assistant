import aws_cdk as cdk

from infrastructure.stack import HouseholdBudgetStack

app = cdk.App()
HouseholdBudgetStack(app, "HouseholdBudgetMessaging")
app.synth()
