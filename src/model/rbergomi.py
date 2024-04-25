import numpy as np
from qablet.base.mc import MCModel, MCStateBase
from numpy.random import Generator, SFC64
from qablet.base.utils import Forwards


def g(x, a):
    """
    TBSS kernel applicable to the rBergomi variance process.
    """
    return x**a


def b(k, a):
    """
    Optimal discretisation of TBSS process for minimising hybrid scheme error.
    """
    return ((k ** (a + 1) - (k - 1) ** (a + 1)) / (a + 1)) ** (1 / a)


def cov(a, dt):
    """
    Covariance matrix for given alpha and n, assuming kappa = 1 for tractability.
    """
    cov = np.array([[0.0, 0.0], [0.0, 0.0]])
    cov[0, 0] = dt
    cov[0, 1] = (dt ** (1.0 * a + 1)) / (1.0 * a + 1)
    cov[1, 1] = (dt ** (2.0 * a + 1)) / (2.0 * a + 1)
    cov[1, 0] = cov[0, 1]
    return cov


class rBergomiMCState(MCStateBase):
    """
    Monte Carlo Implementation of the rBergomi model using the scheme proposed by
    Mikkel Bennedsen, Asger Lunde, and Mikko S Pakkanen.,
    "Hybrid scheme for Brownian semistationary processes.",
    Finance and Stochastics, 21(4): 931-965, 2017.
    """

    def __init__(self, timetable, dataset):
        super().__init__(timetable, dataset)

        # Fetch the common model parameters from the dataset
        self.n = dataset["MC"]["PATHS"]
        self.asset = dataset["rB"]["ASSET"]
        self.asset_fwd = Forwards(dataset["ASSETS"][self.asset])
        self.spot = self.asset_fwd.forward(0)

        # Fetch the rBergomi parameters from the dataset
        self.a = dataset["rB"]["ALPHA"]
        self.rho = dataset["rB"]["RHO"]
        self.rho_comp = np.sqrt(1 - self.rho**2)
        self.xi = dataset["rB"]["XI"]
        self.eta = dataset["rB"]["ETA"]

        # Initialize rng, and log stock and variance processes
        self.rng = Generator(SFC64(dataset["MC"]["SEED"]))
        self.x_vec = np.zeros(self.n)  # log stock process
        self.V = np.zeros(self.n)  # variance process
        self.V.fill(self.xi)

        # Preallocate arrays for the convolution (for 1000 timesteps)
        self.G = np.zeros(1000)
        self.X = np.zeros((self.n, 1000))
        self.k = 0  # step counter
        self.mean = np.array([0, 0])

        self.cur_time = 0

    def advance(self, new_time):
        """Update x_vec in place when we move simulation by time dt."""

        dt = new_time - self.cur_time
        if dt < 1e-10:
            return

        # generate the random numbers for the hybrid scheme
        dwv = self.rng.multivariate_normal(
            self.mean, cov(self.a, dt), self.n
        ).transpose()

        # Update the log stock process first,
        # using a Wiener process correlated to dwv[0]
        dws = self.rng.normal(0, np.sqrt(dt) * self.rho_comp, self.n)
        dws += self.rho * dwv[0]

        fwd_rate = self.asset_fwd.rate(new_time, self.cur_time)

        self.x_vec += (fwd_rate - self.V / 2.0) * dt
        self.x_vec += np.sqrt(self.V) * dws

        # First part of variance: Exact integrals
        YE = dwv[1]

        # Second part of variance: Riemann sums using convolution
        # Construct arrays for convolution
        self.X[:, self.k] = dwv[0]  # Xi
        self.k += 1
        if self.k > 1:
            self.G[self.k] = g(b(self.k, self.a) * dt, self.a)
        # Convolution
        YR = np.dot(self.X[:, : self.k - 1], self.G[self.k : 1 : -1])

        # Finally construct and return full process
        Y = np.sqrt(2 * self.a + 1) * (YE + YR)

        self.V = self.xi * np.exp(
            self.eta * Y - 0.5 * self.eta**2 * new_time ** (2 * self.a + 1)
        )

        self.cur_time = new_time

    def get_value(self, unit):
        """Return the value of the unit at the current time.
        This model uses black scholes model for one asset, return its value using the simulated array.
        For any other asset that may exist in the timetable, just return the default implementation in
        the model base (i.e. simply return the forwards)."""

        if unit == self.asset:
            return self.spot * np.exp(self.x_vec)
        else:
            return None


class rBergomiMCModel(MCModel):
    def state_class(self):
        return rBergomiMCState


if __name__ == "__main__":
    import pyarrow as pa
    from qablet_contracts.timetable import py_to_ts, TS_EVENT_SCHEMA
    from datetime import datetime

    model = rBergomiMCModel()

    times = np.array([0.0, 1.0, 2.0, 5.0])
    rates = np.array([0.04, 0.04, 0.045, 0.05]) * 0.0
    discount_data = ("ZERO_RATES", np.column_stack((times, rates)))
    div_rate = 0.0
    fwds = 100 * np.exp((rates - div_rate) * times)
    fwd_data = ("FORWARDS", np.column_stack((times, fwds)))
    ticker = "SPX"

    dataset = {
        "BASE": "USD",
        "PRICING_TS": py_to_ts(datetime(2023, 12, 31)).value,
        "ASSETS": {"USD": discount_data, ticker: fwd_data},
        "MC": {
            "PATHS": 100_000,
            "TIMESTEP": 1 / 100,
            "SEED": 1,
        },
        "rB": {"ASSET": "SPX", "ALPHA": -0.45, "RHO": -0.8, "XI": 0.11, "ETA": 2.5},
    }

    # We will define a forward timetable, instead of using contract classes from qablet_contracts
    events = [
        {
            "track": "",
            "time": datetime(2024, 1, 31),
            "op": "+",
            "quantity": 1,
            "unit": ticker,
        }
    ]

    events_table = pa.RecordBatch.from_pylist(events, schema=TS_EVENT_SCHEMA)
    fwd_timetable = {"events": events_table, "expressions": {}}
    print(fwd_timetable["events"].to_pandas())

    price, stats = model.price(fwd_timetable, dataset)
    print(price)