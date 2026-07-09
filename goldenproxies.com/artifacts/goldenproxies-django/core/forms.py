from django import forms
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.contrib.auth.models import User
from .models import SupportMessage


INPUT_CLS = (
    "w-full px-4 py-3 rounded-xl border border-[#D4AF37]/20 "
    "bg-gradient-to-br from-white/38 to-[#FFFCF0]/18 "
    "backdrop-blur-[10px] saturate-150 "
    "text-gray-800 placeholder-gray-400 "
    "shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] "
    "focus:outline-none focus:border-[#D4AF37]/50 "
    "focus:ring-2 focus:ring-[#D4AF37]/15 focus:bg-white/50 transition"
)


class RegisterForm(UserCreationForm):
    first_name = forms.CharField(
        max_length=30,
        widget=forms.TextInput(attrs={"placeholder": "First name", "class": INPUT_CLS}),
    )
    last_name = forms.CharField(
        max_length=30,
        widget=forms.TextInput(attrs={"placeholder": "Last name", "class": INPUT_CLS}),
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"placeholder": "Email address", "class": INPUT_CLS}),
    )
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(attrs={"placeholder": "Password", "class": INPUT_CLS}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        widget=forms.PasswordInput(attrs={"placeholder": "Confirm password", "class": INPUT_CLS}),
    )

    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "password1", "password2")

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(username__iexact=email).exists() or User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists. Please sign in instead.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.username = self.cleaned_data["email"]
        user.email = self.cleaned_data["email"]
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        if commit:
            user.save()
        return user


class LoginForm(AuthenticationForm):
    username = forms.CharField(
        label="Email",
        widget=forms.TextInput(attrs={"placeholder": "Email address", "class": INPUT_CLS, "autofocus": True}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Password", "class": INPUT_CLS}),
    )


class LoginCodeForm(forms.Form):
    code = forms.CharField(
        min_length=6,
        max_length=6,
        widget=forms.TextInput(attrs={
            "placeholder": "6-digit code",
            "class": INPUT_CLS + " text-center tracking-[0.35em] font-mono",
            "inputmode": "numeric",
            "autocomplete": "one-time-code",
        }),
    )

    def clean_code(self):
        code = self.cleaned_data["code"].strip().replace(" ", "")
        if not code.isdigit():
            raise forms.ValidationError("Enter the 6-digit code from your email.")
        return code


SELECT_CLS = (
    "w-full px-4 py-3 rounded-xl border border-[#D4AF37]/20 "
    "bg-gradient-to-br from-white/38 to-[#FFFCF0]/18 "
    "backdrop-blur-[10px] saturate-150 "
    "text-gray-800 "
    "shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] "
    "focus:outline-none focus:border-[#D4AF37]/50 "
    "focus:ring-2 focus:ring-[#D4AF37]/15 transition"
)

PRIORITY_ICONS = {"critical": "🔴", "high": "🟠", "normal": "🟡", "low": "🟢"}


class SupportForm(forms.ModelForm):
    subject = forms.CharField(
        max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "e.g. Question about my proxies", "class": INPUT_CLS}),
    )
    priority = forms.ChoiceField(
        choices=[("critical", "🔴 Critical"), ("high", "🟠 High"), ("normal", "🟡 Normal"), ("low", "🟢 Low")],
        initial="normal",
        widget=forms.Select(attrs={"class": SELECT_CLS}),
    )
    body = forms.CharField(
        widget=forms.Textarea(
            attrs={"placeholder": "Describe your issue or question...", "rows": 5, "class": INPUT_CLS + " resize-none"}
        ),
    )

    class Meta:
        model = SupportMessage
        fields = ["subject", "priority", "body"]


class AdminReplyForm(forms.Form):
    reply_body = forms.CharField(
        widget=forms.Textarea(
            attrs={"placeholder": "Write your reply...", "rows": 4, "class": INPUT_CLS + " resize-none"}
        ),
    )
